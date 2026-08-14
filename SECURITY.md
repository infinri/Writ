# Security Policy

## Reporting a vulnerability

Report privately through [GitHub Security Advisories](https://github.com/infinri/Writ/security/advisories/new). Please do not open a public issue for anything exploitable.

Useful things to include: what you did, what happened, what you expected, the Writ commit you are on (`git rev-parse --short HEAD`), and your Claude Code version. A minimal reproduction beats a description.

Expect an acknowledgement within a few days. Writ is maintained by one person in their own time, so please size your expectations to that rather than to a vendor SLA. If a report is valid and you want credit in the advisory, say so.

## Supported versions

The latest released version on the `main` branch. There are no long-term support branches and no backported fixes; a security fix ships in the next release.

## What Writ actually is, in security terms

Read this before you decide how much to trust it. Writ is not a sandbox and its gates are not a security boundary.

**It runs shell scripts with your privileges.** Writ installs about 44 hook registrations that Claude Code invokes on your behalf. Those hooks are bash, they run as your user, and they read and write inside your repositories. Anything your shell can do, a hook can do.

**The daemon has no authentication.** `writ-server` listens on port 8765 and every route is unauthenticated. The bind address is the whole access control, and it defaults to `localhost`. Any process on the machine that can reach that port can read and modify session state, gate approvals, and the rule corpus, so treat a shared or multi-tenant host as one where Writ's session state is readable by anyone on it.

The bind address is configurable through `WRIT_HOST` (see `scripts/install-server-service.sh`). **Setting it to a non-loopback address publishes an unauthenticated read-write API to that network.** There is no auth layer behind it to fall back on. Do not do this on any network you do not fully control, and understand that "fully control" includes every other container and VM on the same bridge.

**The gates protect against mistakes, not adversaries.** Writ's write gates, mode gates, and approval gates exist to stop a capable assistant from doing something careless: overwriting a file it should not, skipping a plan, wiping a graph. They are guardrails against error. They are not designed to withstand a determined attacker or a prompt-injection payload aiming to get around them, and they have documented limits. The Bash write gate, for example, inspects redirects, copy destinations, and interpreter one-liners (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`), and its own source names what it does not catch, including `python -m MODULE`, `sh -c` wrappers, values assembled through shell variables, and `eval`. A missing gate decision is a gap in the guardrail, and worth reporting. It is not a sandbox escape, because there is no sandbox.

**Credentials.** Neo4j credentials resolve from environment variables, then `writ.toml`, then a built-in default (`writ/config.py`). That built-in default is a development password published in this repository, so a Writ install that was never configured is running on a password anyone can read here. If your Neo4j instance is reachable by anything other than you, set a real one via `WRIT_NEO4J_PASSWORD` or `writ.toml`. `writ doctor` reports which configuration keys are present and never returns, logs, or prints a credential value (`writ/session/doctor.py`).

**What Writ stores.** The rule corpus, session state, decision records, and logs go into Neo4j and into `var/` inside the installation. Session logs can contain file paths, command text, and excerpts of your code. Before publishing a graph dump, an audit log, or a benchmark result, read it. Paths and command lines carry more about your environment and your employer than people expect.

## What leaves your machine

Short answer: your rules and your code do not. The longer answer, because "nothing is sent anywhere" is the kind of absolute worth checking rather than trusting.

**Stays local, always.** The rule corpus, the graph database, session state, decision records, and every log stream. Neo4j runs in a container on your machine and the daemon binds loopback. There is no telemetry, no analytics, no usage reporting, and no phone-home of any kind. Nothing about your code, your prompts, or your session is transmitted for the project's benefit.

**Goes out, opt-in, one destination.** The decision-memory PR sync posts per-file reasoning to Bitbucket Cloud, and `api.bitbucket.org` is the only host that client ever contacts (`writ/session/bitbucket_client.py`). It is off unless you put a token in `writ.toml`, and it is the only outbound network call Writ itself makes at runtime. Self-hosted Bitbucket Server is refused outright rather than half-supported. There is no GitHub client, so on a GitHub-hosted project this channel does nothing.

**Goes out once, at install.** Bootstrap downloads the embedding model (`sentence-transformers/all-MiniLM-L6-v2`) and the Python dependencies. Ordinary package and model fetches, and after that the embedding runs locally.

**Not Writ, but worth knowing.** Your AI assistant has its own network access and its own tools. If it searches the web or fetches a URL, that is the assistant acting, not Writ, and Writ neither performs nor prevents it. What Writ does add is a Bash egress guard that questions outbound commands to hosts you have not allowlisted (`[egress] allow_hosts` in `writ.toml`), which narrows one channel without pretending to close all of them.

## Your responsibility

**Audit anything you install, including this.** Writ is a tool that hands an AI assistant hooks into your shell and your repositories. Before you run it, or any other plugin, skill, agent, or MCP server, read what it does. Check what it executes, what it sends over the network, and what it writes. Clone it and look, rather than trusting a description, a star count, or a summary that something else generated for you.

**AI assistance is not a substitute for judgement.** An assistant, this project included, can be confidently wrong, can miss what it was not looking for, and can produce a clean-looking review of code it did not properly read. "The AI said it was fine", "the AI wrote it", and "the AI reviewed it" are not answers to a security question, and they will not be answers to your users, your employer, or your auditor. The responsibility for what runs on your machine and ships to production stays with the person who ran it. Writ exists to relocate oversight, not to remove it, and a tool that helps you check your work does not transfer the accountability for that work.

If you are using Writ in an environment where a mistake is expensive, treat its output as a draft that needs your review, keep the human approval gates on, and read the diffs.
