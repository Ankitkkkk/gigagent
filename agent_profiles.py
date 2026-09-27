"""Versioned, immutable saved instructions for terminal agents."""
from copy import deepcopy

CATALOG_VERSION = 1
_ROLES = {
    'generalist': 'Handle varied requests. Clarify material ambiguity and verify your work.',
    'implementer': 'Implement requested changes with focused scope and appropriate verification.',
    'code-reviewer': 'Review changes for correctness, regressions, and missing coverage. Report concrete findings.',
    'planner': 'Explore requirements and dependencies. Produce actionable plans with verification steps.',
    'tester': 'Identify risks, exercise behavior, and build meaningful regression coverage.',
    'debugger': 'Reproduce failures, gather evidence, isolate root causes, and verify fixes.',
}
_PERSONALITIES = {
    'pragmatic': 'Prefer practical solutions and explain meaningful tradeoffs.',
    'meticulous': 'Check assumptions and edge cases carefully; keep evidence precise.',
    'concise': 'Use brief, direct communication while retaining essential evidence.',
    'supportive': 'Communicate patiently and constructively; explain useful next steps.',
}
ROLE_CHOICES = tuple(_ROLES)
PERSONALITY_CHOICES = tuple(_PERSONALITIES)


ORCHESTRATOR_INSTRUCTIONS = (' You are this session\'s orchestrator. Route requests only; do not implement tasks. '
                "Call chat_orchestrate(action='pending') to read pending requests and current worker roster. "
                "Choose suitable workers, then call chat_orchestrate(action='route', message_id=<integer>, "
                'agent_ids=<list of stable worker IDs>, reason=<string>). '
                'Use only server-provided IDs. Wait for server notifications; do not poll. ')


def make_profile(role='generalist', personality='pragmatic') -> dict:
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError(f'role must be one of {ROLE_CHOICES}')
    if not isinstance(personality, str) or personality not in _PERSONALITIES:
        raise ValueError(f'personality must be one of {PERSONALITY_CHOICES}')
    return dict(role=role, personality=personality, catalog_version=CATALOG_VERSION,
                role_instructions=_ROLES[role], personality_instructions=_PERSONALITIES[personality])


def validate_profile(profile) -> dict:
    if not isinstance(profile, dict) or set(profile) != {
            'role', 'personality', 'catalog_version', 'role_instructions', 'personality_instructions'}:
        raise ValueError('invalid profile snapshot')
    if (profile['role'] not in ROLE_CHOICES or profile['personality'] not in PERSONALITY_CHOICES
            or type(profile['catalog_version']) is not int or profile['catalog_version'] < 1
            or any(not isinstance(profile[key], str) or not profile[key].strip()
                   for key in ('role_instructions', 'personality_instructions'))):
        raise ValueError('invalid profile snapshot')
    return deepcopy(profile)


def profile_prompt(agent) -> str:
    profile = agent.get('profile')
    if not profile:
        return ''
    if agent.get('kind') == 'orchestrator':
        return ' Fixed session orchestrator instructions: ' + profile['role_instructions'] + ' '
    return (f" Saved role: {profile['role']}. {profile['role_instructions']} "
            f"Saved personality: {profile['personality']}. {profile['personality_instructions']} ")
