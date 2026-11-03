"""Whole-submission assessment and MCA scoring: the request boundary between
the reviewer workspace and the model.

:func:`build_snapshot` freezes a submission at Submit: its canonical answers,
their serialized text, the input digest, the dimension and matrix
definitions, and the template, set and model identities every later call is
checked against. :func:`assess_dimension` drafts one dimension's finding and
:func:`score_criterion` one MCA criterion's score.

Each call trusts nothing in its request. It re-validates the request, then
checks it against the running platform in a fixed order — the template, the
set, the dimension or criterion, the template and set versions, the model,
and the input digest recomputed from a fresh serialization of the submission
— before any model call. A mismatch is a ``terminal`` error: the platform no
longer matches what the review recorded, so a retry cannot succeed. The
model's reply is validated against the submission (a citation of a field
the submission lacks, or a ``not_evidenced`` score above the scale minimum,
rejects the whole reply) and each citation's quote is verified against the
cited field's text. A failed call or rejected reply is a ``retryable``
``provider_error``. Neither call raises: every failure is a value carrying a
user-facing message and a ``{kind, code}`` error.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from pcn_clone.ai.config import AiConfigurationError
from pcn_clone.ai.evaluate import NOT_CONFIGURED_MESSAGE
from pcn_clone.ai.prompts import assessment_prompts, mca_prompts
from pcn_clone.ai.provider import ModelClient, ProviderError, shared_client
from pcn_clone.ai.schemas import (
    ASSESSMENT_FINDING_FORMAT,
    MCA_SCORE_FINDING_FORMAT,
    AssessmentFinding,
    Citation,
    DocumentedBasis,
    McaScoreFinding,
    ReplyScope,
    Verdict,
    assessment_finding_schema,
    mca_score_finding_schema,
)
from pcn_clone.content import load_assessment, load_mca, load_template
from pcn_clone.content.models import (
    AssessmentDimension,
    McaMatrix,
    NonEmptyStr,
    Submission,
    SubmissionLike,
    Template,
)
from pcn_clone.core.evidence import verify_quote
from pcn_clone.core.serialize import (
    CanonicalSubmission,
    SerializedField,
    canonicalize_submission,
    input_digest,
    serialize_submission,
)

logger = logging.getLogger(__name__)

ASSESSMENT_ERROR_MESSAGE = "The assessment could not be completed. Please try again."
"""The user-facing message when a dimension's model call or reply fails."""

SCORING_ERROR_MESSAGE = "The scoring could not be completed. Please try again."
"""The user-facing message when a criterion's model call or reply fails."""

INPUT_DIGEST_MISMATCH_MESSAGE = (
    "The platform no longer serializes this submission to the text this "
    "review recorded."
)

ErrorKind = Literal["retryable", "terminal"]

ErrorCode = Literal[
    "invalid_request",
    "unknown_template",
    "unknown_assessment_set",
    "unknown_dimension",
    "unknown_mca_set",
    "unknown_mca_criterion",
    "template_version_mismatch",
    "assessment_set_version_mismatch",
    "mca_set_version_mismatch",
    "model_mismatch",
    "inconsistent_submission",
    "input_digest_mismatch",
    "provider_error",
]
"""Every code a call returns. ``provider_error`` is the one retryable code;
the rest are terminal."""


@dataclass(frozen=True, slots=True)
class RunError:
    """Why a call failed: its ``kind`` and ``code``."""

    kind: ErrorKind
    code: ErrorCode

    @classmethod
    def of(cls, code: ErrorCode) -> RunError:
        """The error for ``code``, retryable only for ``provider_error``."""
        return cls("retryable" if code == "provider_error" else "terminal", code)

    def to_json(self) -> dict[str, str]:
        """``{kind, code}``."""
        return {"kind": self.kind, "code": self.code}


# --- Requests ----------------------------------------------------------------


class _Request(BaseModel):
    """The pins shared by both requests; any unknown key is rejected."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )

    template_id: NonEmptyStr
    template_version: NonEmptyStr
    model: NonEmptyStr
    assessment_input_digest: NonEmptyStr
    submission: Submission


class AssessDimensionRequest(_Request):
    """One dimension-assessment request: ``{templateId, templateVersion,
    assessmentSetId, assessmentSetVersion, model, assessmentInputDigest,
    dimensionId, submission}``, the submission being the canonical one the
    review recorded."""

    assessment_set_id: NonEmptyStr
    assessment_set_version: NonEmptyStr
    dimension_id: NonEmptyStr


class ScoreCriterionRequest(_Request):
    """One criterion-scoring request: ``{templateId, templateVersion,
    mcaSetId, mcaSetVersion, model, assessmentInputDigest, criterionId,
    submission}``. MCA reads the same serialized submission as the
    assessment, so the assessment input digest pins its input."""

    mca_set_id: NonEmptyStr
    mca_set_version: NonEmptyStr
    criterion_id: NonEmptyStr


R = TypeVar("R", bound=_Request)


def _validated(request_type: type[R], request: Any) -> R | str:
    """The validated request, or the ``Invalid request — …`` message."""
    if isinstance(request, request_type):
        return request
    try:
        return request_type.model_validate(request)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: "
            f"{issue['msg']}"
            for issue in error.errors()
        )
        return f"Invalid request — {problems}"


def _read_string(request: Any, key: str) -> str:
    """A string property of an unvalidated request, or ``""``."""
    if isinstance(request, Mapping):
        value = request.get(key)
        if isinstance(value, str):
            return value
    return ""


# --- Findings ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedCitation:
    """A citation and whether its quote occurs in the cited field's text."""

    section_id: str
    field_id: str
    quote: str
    verified: bool

    def to_json(self) -> dict[str, Any]:
        """``{sectionId, fieldId, quote, verified}``."""
        return {
            "sectionId": self.section_id,
            "fieldId": self.field_id,
            "quote": self.quote,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class DimensionFinding:
    """A drafted finding for one assessment dimension."""

    dimension_id: str
    model: str
    verdict: Verdict
    reasoning: str
    evidence: tuple[VerifiedCitation, ...]
    questions: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """``{dimensionId, model, verdict, reasoning, evidence: [{sectionId,
        fieldId, quote, verified}], questions}``."""
        return {
            "dimensionId": self.dimension_id,
            "model": self.model,
            "verdict": self.verdict,
            "reasoning": self.reasoning,
            "evidence": [citation.to_json() for citation in self.evidence],
            "questions": list(self.questions),
        }


@dataclass(frozen=True, slots=True)
class ScoreFinding:
    """A drafted score for one MCA criterion. A ``documented`` basis carries
    1–3 citations; a ``not_evidenced`` one carries none and scores the scale
    minimum."""

    criterion_id: str
    model: str
    score: int
    justification: str
    basis: Literal["documented", "not_evidenced"]
    evidence: tuple[VerifiedCitation, ...]

    def to_json(self) -> dict[str, Any]:
        """``{criterionId, model, score, justification, basis}``, the basis
        being ``{kind: "documented", evidence: [...]}`` or ``{kind:
        "not_evidenced"}``."""
        basis: dict[str, Any] = {"kind": self.basis}
        if self.basis == "documented":
            basis["evidence"] = [citation.to_json() for citation in self.evidence]
        return {
            "criterionId": self.criterion_id,
            "model": self.model,
            "score": self.score,
            "justification": self.justification,
            "basis": basis,
        }


def _verified(
    citations: Sequence[Citation], fields: Sequence[SerializedField]
) -> tuple[VerifiedCitation, ...]:
    """The citations with their quotes checked against the cited fields,
    each of which the reply's validation has already found in ``fields``."""
    text_by_ref = {(f.section_id, f.field_id): f.serialized_text for f in fields}
    return tuple(
        VerifiedCitation(
            section_id=citation.section_id,
            field_id=citation.field_id,
            quote=citation.quote,
            verified=verify_quote(
                citation.quote, text_by_ref[(citation.section_id, citation.field_id)]
            ),
        )
        for citation in citations
    )


# --- Responses ---------------------------------------------------------------


def _failure_json(error_message: str | None, error: RunError | None) -> dict[str, Any]:
    return {
        "errorMessage": error_message or "",
        "error": error.to_json() if error is not None else None,
    }


@dataclass(frozen=True, slots=True)
class AssessDimensionResponse:
    """The outcome of one dimension assessment: ``ok`` with ``finding``, or
    a user-facing ``error_message`` and its ``error``. ``dimension_id``
    echoes the request."""

    ok: bool
    dimension_id: str
    finding: DimensionFinding | None = None
    error_message: str | None = None
    error: RunError | None = None

    def to_json(self) -> dict[str, Any]:
        """``{ok: true, dimensionId, finding}`` or ``{ok: false, dimensionId,
        errorMessage, error: {kind, code}}``."""
        if self.ok and self.finding is not None:
            return {
                "ok": True,
                "dimensionId": self.dimension_id,
                "finding": self.finding.to_json(),
            }
        return {
            "ok": False,
            "dimensionId": self.dimension_id,
            **_failure_json(self.error_message, self.error),
        }


@dataclass(frozen=True, slots=True)
class ScoreCriterionResponse:
    """The outcome of one criterion scoring: ``ok`` with ``finding``, or a
    user-facing ``error_message`` and its ``error``. ``criterion_id`` echoes
    the request."""

    ok: bool
    criterion_id: str
    finding: ScoreFinding | None = None
    error_message: str | None = None
    error: RunError | None = None

    def to_json(self) -> dict[str, Any]:
        """``{ok: true, criterionId, finding}`` or ``{ok: false, criterionId,
        errorMessage, error: {kind, code}}``."""
        if self.ok and self.finding is not None:
            return {
                "ok": True,
                "criterionId": self.criterion_id,
                "finding": self.finding.to_json(),
            }
        return {
            "ok": False,
            "criterionId": self.criterion_id,
            **_failure_json(self.error_message, self.error),
        }


# --- The intake snapshot -----------------------------------------------------


def _configured_model() -> str:
    """The model the configured provider runs. Raises
    :class:`AiConfigurationError` when the AI settings are unusable."""
    return shared_client().model


def _field_json(field: SerializedField) -> dict[str, str]:
    return {
        "sectionId": field.section_id,
        "sectionLabel": field.section_label,
        "fieldId": field.field_id,
        "fieldLabel": field.field_label,
        "serializedText": field.serialized_text,
    }


@dataclass(frozen=True, slots=True)
class IntakeSnapshot:
    """A submission frozen for review: everything the reviewer workspace
    displays and every pin the assessment and scoring requests carry."""

    template_id: str
    template_label: str
    template_version: str
    assessment_set_id: str
    assessment_set_version: str
    mca_set_id: str
    mca_set_version: str
    model: str
    assessment_input_digest: str
    submission: CanonicalSubmission
    fields: tuple[SerializedField, ...]
    dimension_defs: tuple[AssessmentDimension, ...]
    matrix: McaMatrix

    def to_json(self) -> dict[str, Any]:
        """``{templateId, templateLabel, templateVersion, assessmentSetId,
        assessmentSetVersion, mcaSetId, mcaSetVersion, model,
        assessmentInputDigest, submission, fields: [{sectionId,
        sectionLabel, fieldId, fieldLabel, serializedText}], dimensionDefs:
        [{id, label, description, guidance, verdicts: {fail, notClear,
        pass}}], matrix: {scale, bands, criteria}}``."""
        return {
            "templateId": self.template_id,
            "templateLabel": self.template_label,
            "templateVersion": self.template_version,
            "assessmentSetId": self.assessment_set_id,
            "assessmentSetVersion": self.assessment_set_version,
            "mcaSetId": self.mca_set_id,
            "mcaSetVersion": self.mca_set_version,
            "model": self.model,
            "assessmentInputDigest": self.assessment_input_digest,
            "submission": copy.deepcopy(self.submission),
            "fields": [_field_json(field) for field in self.fields],
            "dimensionDefs": [dimension.to_json() for dimension in self.dimension_defs],
            "matrix": self.matrix.to_json(),
        }


def build_snapshot(submission: SubmissionLike) -> IntakeSnapshot:
    """Freeze a submission for review: validate it, canonicalize it against
    the template (unanswered fields become explicit blanks), serialize it,
    digest the serialization, and record the template, set and model
    identities in force.

    Raises ``ValueError`` when the submission is malformed or does not belong
    to the template, and :class:`AiConfigurationError` when the AI settings
    are unusable.
    """
    template = load_template()
    assessment_set = load_assessment()
    mca_set = load_mca()
    validated = (
        submission
        if isinstance(submission, Submission)
        else Submission.model_validate(submission)
    )
    canonical = canonicalize_submission(template, validated)
    fields = serialize_submission(template, canonical)
    return IntakeSnapshot(
        template_id=template.id,
        template_label=template.label,
        template_version=template.version,
        assessment_set_id=assessment_set.id,
        assessment_set_version=assessment_set.version,
        mca_set_id=mca_set.id,
        mca_set_version=mca_set.version,
        model=_configured_model(),
        assessment_input_digest=input_digest(fields),
        submission=canonical,
        fields=tuple(fields),
        dimension_defs=tuple(assessment_set.dimensions),
        matrix=mca_set.matrix,
    )


# --- Shared checks -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rejection:
    code: ErrorCode
    message: str


def _checked_fields(
    template: Template, request: _Request, verb: str
) -> list[SerializedField] | _Rejection:
    """The submission serialized afresh, once the configured model and the
    recomputed input digest both match the request."""
    try:
        model = _configured_model()
    except AiConfigurationError as error:
        logger.error("AI review is not configured: %s", error)
        return _Rejection("provider_error", NOT_CONFIGURED_MESSAGE)
    if model != request.model:
        return _Rejection(
            "model_mismatch",
            f'The platform now {verb} with "{model}"; this review was created '
            f'against "{request.model}".',
        )
    try:
        fields = serialize_submission(
            template, canonicalize_submission(template, request.submission)
        )
    except Exception as error:
        return _Rejection("inconsistent_submission", str(error))
    if input_digest(fields) != request.assessment_input_digest:
        return _Rejection("input_digest_mismatch", INPUT_DIGEST_MISMATCH_MESSAGE)
    return fields


def _log_failure(subject: str, error: Exception) -> None:
    if isinstance(error, ProviderError):
        logger.error("%s failed (%s): %s", subject, error.kind, error.message)
    else:
        logger.error("%s failed", subject, exc_info=error)


# --- Assessment --------------------------------------------------------------


async def assess_dimension(
    request: AssessDimensionRequest | Mapping[str, Any],
    *,
    client: ModelClient | None = None,
) -> AssessDimensionResponse:
    """Draft the finding for one dimension of a submission.

    ``request`` is an :class:`AssessDimensionRequest` or its JSON shape.
    ``client`` defaults to the process's shared client.
    """
    parsed = _validated(AssessDimensionRequest, request)
    if isinstance(parsed, str):
        return AssessDimensionResponse(
            ok=False,
            dimension_id=_read_string(request, "dimensionId"),
            error_message=parsed,
            error=RunError.of("invalid_request"),
        )
    dimension_id = parsed.dimension_id

    def failure(code: ErrorCode, message: str) -> AssessDimensionResponse:
        return AssessDimensionResponse(
            ok=False,
            dimension_id=dimension_id,
            error_message=message,
            error=RunError.of(code),
        )

    template = load_template()
    if parsed.template_id != template.id:
        return failure(
            "unknown_template",
            f'Template "{parsed.template_id}" is not registered on this platform.',
        )
    assessment_set = load_assessment()
    if parsed.assessment_set_id != assessment_set.id:
        return failure(
            "unknown_assessment_set",
            f'Assessment set "{parsed.assessment_set_id}" is not registered for '
            f'template "{parsed.template_id}".',
        )
    if assessment_set.get_dimension(dimension_id) is None:
        return failure(
            "unknown_dimension",
            f'Assessment set "{assessment_set.id}" has no dimension "{dimension_id}".',
        )
    if template.version != parsed.template_version:
        return failure(
            "template_version_mismatch",
            f'Template "{template.id}" is now version "{template.version}"; this '
            f'review was created against "{parsed.template_version}".',
        )
    if assessment_set.version != parsed.assessment_set_version:
        return failure(
            "assessment_set_version_mismatch",
            f'Assessment set "{assessment_set.id}" is now version '
            f'"{assessment_set.version}"; this review was created against '
            f'"{parsed.assessment_set_version}".',
        )
    fields = _checked_fields(template, parsed, "assesses")
    if isinstance(fields, _Rejection):
        return failure(fields.code, fields.message)

    try:
        system, user = assessment_prompts(
            template, assessment_set, dimension_id, fields
        )
        model_client = client if client is not None else shared_client()
        reply = await model_client.complete(
            system=system,
            user=user,
            schema=assessment_finding_schema(fields),
            format_name=ASSESSMENT_FINDING_FORMAT,
        )
        drafted = AssessmentFinding.model_validate(
            reply.data, context=ReplyScope(fields)
        )
    except Exception as error:
        _log_failure(f'Assessment of dimension "{dimension_id}"', error)
        return failure("provider_error", ASSESSMENT_ERROR_MESSAGE)

    return AssessDimensionResponse(
        ok=True,
        dimension_id=dimension_id,
        finding=DimensionFinding(
            dimension_id=dimension_id,
            model=parsed.model,
            verdict=drafted.verdict,
            reasoning=drafted.reasoning,
            evidence=_verified(drafted.evidence, fields),
            questions=tuple(drafted.questions),
        ),
    )


# --- MCA scoring -------------------------------------------------------------


async def score_criterion(
    request: ScoreCriterionRequest | Mapping[str, Any],
    *,
    client: ModelClient | None = None,
) -> ScoreCriterionResponse:
    """Draft the MCA score for one criterion of a submission.

    ``request`` is a :class:`ScoreCriterionRequest` or its JSON shape.
    ``client`` defaults to the process's shared client.
    """
    parsed = _validated(ScoreCriterionRequest, request)
    if isinstance(parsed, str):
        return ScoreCriterionResponse(
            ok=False,
            criterion_id=_read_string(request, "criterionId"),
            error_message=parsed,
            error=RunError.of("invalid_request"),
        )
    criterion_id = parsed.criterion_id

    def failure(code: ErrorCode, message: str) -> ScoreCriterionResponse:
        return ScoreCriterionResponse(
            ok=False,
            criterion_id=criterion_id,
            error_message=message,
            error=RunError.of(code),
        )

    template = load_template()
    if parsed.template_id != template.id:
        return failure(
            "unknown_template",
            f'Template "{parsed.template_id}" is not registered on this platform.',
        )
    mca_set = load_mca()
    if parsed.mca_set_id != mca_set.id:
        return failure(
            "unknown_mca_set",
            f'MCA set "{parsed.mca_set_id}" is not registered for template '
            f'"{parsed.template_id}".',
        )
    matrix = mca_set.matrix
    if matrix.get_criterion(criterion_id) is None:
        return failure(
            "unknown_mca_criterion",
            f'MCA set "{mca_set.id}" has no criterion "{criterion_id}".',
        )
    if template.version != parsed.template_version:
        return failure(
            "template_version_mismatch",
            f'Template "{template.id}" is now version "{template.version}"; this '
            f'review was created against "{parsed.template_version}".',
        )
    if mca_set.version != parsed.mca_set_version:
        return failure(
            "mca_set_version_mismatch",
            f'MCA set "{mca_set.id}" is now version "{mca_set.version}"; this '
            f'review was created against "{parsed.mca_set_version}".',
        )
    fields = _checked_fields(template, parsed, "scores")
    if isinstance(fields, _Rejection):
        return failure(fields.code, fields.message)

    try:
        system, user = mca_prompts(template, mca_set, criterion_id, fields)
        model_client = client if client is not None else shared_client()
        reply = await model_client.complete(
            system=system,
            user=user,
            schema=mca_score_finding_schema(fields, matrix),
            format_name=MCA_SCORE_FINDING_FORMAT,
        )
        drafted = McaScoreFinding.model_validate(
            reply.data, context=ReplyScope(fields, matrix)
        )
    except Exception as error:
        _log_failure(f'Scoring of criterion "{criterion_id}"', error)
        return failure("provider_error", SCORING_ERROR_MESSAGE)

    basis = drafted.basis
    return ScoreCriterionResponse(
        ok=True,
        criterion_id=criterion_id,
        finding=ScoreFinding(
            criterion_id=criterion_id,
            model=parsed.model,
            score=basis.score,
            justification=drafted.justification,
            basis=basis.kind,
            evidence=(
                _verified(basis.evidence, fields)
                if isinstance(basis, DocumentedBasis)
                else ()
            ),
        ),
    )
