from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F

from posthog.models import Team, User

from ..models.community_skills import CommunitySkill, CommunitySkillVote
from ..models.skills import LLMSkill
from .skill_serializers import validate_skill_name_value
from .skill_services import create_skill
from .skill_template_services import parse_template_variables, render_template_skill


class CommunitySkillNotFoundError(Exception):
    pass


def get_community_skill_by_slug(slug: str) -> CommunitySkill | None:
    return CommunitySkill.objects.filter(slug=slug, deleted=False).first()


def install_community_skill(
    *,
    team: Team,
    user: User,
    slug: str,
    new_name: str | None = None,
    variables: dict[str, str] | None = None,
) -> LLMSkill:
    """Copy a community skill into a team as a regular LLMSkill and bump its install counter.

    When the community skill is a template (its metadata declares `variables`), the user-supplied
    `variables` are bound into the body and bundled files before the LLMSkill is created.

    Raises CommunitySkillNotFoundError if the slug is unknown,
    LLMSkillDuplicateNameConflictError if the target name already exists in the team, and
    MissingTemplateVariableError / UnknownTemplatePlaceholderError on template render failures.
    """
    community_skill = get_community_skill_by_slug(slug)
    if community_skill is None:
        raise CommunitySkillNotFoundError()

    target_name = validate_skill_name_value(new_name or community_skill.slug)

    files = [
        {"path": f.path, "content": f.content, "content_type": f.content_type} for f in community_skill.files.all()
    ]

    # Stamp provenance so an installed skill can be traced back to its community source.
    metadata: dict[str, Any] = {
        **(community_skill.metadata or {}),
        "community_skill_slug": community_skill.slug,
        "community_skill_id": str(community_skill.id),
    }
    body = community_skill.body

    template_variables = parse_template_variables(community_skill.metadata)
    if template_variables:
        rendered = render_template_skill(
            variables=template_variables,
            body=community_skill.body,
            files=files,
            supplied=variables,
        )
        body = rendered.body
        files = rendered.files
        # The instantiated skill is a concrete skill, not a template — drop the variable schema and
        # record what it was rendered from so a re-render stays deterministic.
        metadata.pop("variables", None)
        metadata["instantiated_from"] = f"{community_skill.slug}@{community_skill.source_sha}"
        metadata["variable_bindings"] = rendered.bindings

    installed = create_skill(
        team,
        user=user,
        name=target_name,
        description=community_skill.description,
        body=body,
        license=community_skill.license,
        compatibility=community_skill.compatibility,
        allowed_tools=community_skill.allowed_tools,
        metadata=metadata,
        files=files or None,
    )

    CommunitySkill.objects.filter(pk=community_skill.pk).update(install_count=F("install_count") + 1)
    return installed


def toggle_community_skill_vote(*, slug: str, user: User) -> tuple[int, bool]:
    """Add or remove the user's upvote. Returns (vote_count, has_voted) after the toggle."""
    community_skill = get_community_skill_by_slug(slug)
    if community_skill is None:
        raise CommunitySkillNotFoundError()

    with transaction.atomic():
        existing = CommunitySkillVote.objects.filter(skill=community_skill, user=user).first()
        if existing is not None:
            existing.delete()
            has_voted = False
        else:
            try:
                CommunitySkillVote.objects.create(skill=community_skill, user=user)
                has_voted = True
            except IntegrityError:
                # A concurrent request created the vote first — converge on the "voted" state
                # rather than surfacing the unique-constraint violation.
                has_voted = True
        vote_count = CommunitySkillVote.objects.filter(skill=community_skill).count()

    return vote_count, has_voted
