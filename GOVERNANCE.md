# Writ project governance

## Maintainer and authority

Writ is currently maintained by Lucio Saldivar, GitHub account [infinri](https://github.com/infinri).

The maintainer decides which contributions are accepted, approves releases, manages repository access and coordinates security responses. Contributors retain credit for their work. Sponsorship does not purchase control over releases, security decisions or the roadmap.

This policy describes project decision-making. It does not change Writ's license or its technical approval mechanisms.

## Contributions and decisions

Propose changes through issues and pull requests. Follow [CONTRIBUTING.md](CONTRIBUTING.md) for rule authoring, review and validation.

Material changes to architecture, security boundaries or compatibility should explain the problem, alternatives, evidence and resulting behavior. Record durable architectural decisions under `docs/adr/`. Acceptance of a proposal is not proof that its claims are correct.

The maintainer reviews contributions for fit, maintainability and relevant validation. For changes needing expertise beyond the maintainer's own, seek outside review and describe any remaining uncertainty. A single-maintainer project cannot promise independent review of every change.

## Disagreements

Raise a specific concern in the relevant issue or pull request and explain the evidence or alternative. The maintainer should explain the final decision. New evidence can reopen a decision; repeated demands without new evidence do not require repeated review.

Community participation follows [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Technical disagreement and criticism are welcome.

## Security decisions

Report potentially exploitable issues privately as described in [SECURITY.md](SECURITY.md). Do not publish sensitive reproductions in ordinary issues.

The maintainer coordinates investigation, fixes, advisories and credit. Public documentation should distinguish implemented controls, measured outcomes and unsupported boundaries. The security policy defines the supported threat model.

## Releases and access

The maintainer authorizes release tags and repository access. Release changes should include relevant validation and compatibility information. Required checks and access controls are repository settings and must be verified separately from this policy.

Additional maintainers may be appointed after sustained contributions and explicit agreement on responsibilities. Record changes to authority in this file and grant only the permissions needed.

## Continuity

If the maintainer intends to step down, announce that intention and seek a willing successor with a documented handover. If no successor is available, explain the maintenance status and consider archiving the repository. This policy does not guarantee succession, response times or ongoing support.

## Policy changes

Propose changes to this policy through a pull request with a rationale. The maintainer approves changes and keeps the named maintainers and responsibilities current.
