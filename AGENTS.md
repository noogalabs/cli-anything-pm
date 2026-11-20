# Agent instructions

This is the canonical instruction file for agents in this repository.
Read [the product specification](docs/SPEC.md) before changing behavior.

## Read first

1. Read [README.md](README.md) for installation, configuration and command limits.
2. Read [PROPERTYMELD.md](PROPERTYMELD.md) for account access and request behavior.
3. Read [the packaged skill](cli_anything/propertymeld/skills/SKILL.md) for command examples.
4. Check the installed command help before using an example.

The command-line tool, or CLI, lets an agent use Property Meld from a terminal.
Nexus is the Property Meld API, a service that accepts program requests.
A session stores sign-in information for the account website.
Nexus access and a valid website session are separate requirements.

## How to talk with your member

Follow ASD-STE100 Simplified Technical English in every interaction.
Apply these rules when answering questions, setting up the tool and doing daily work.
This is a writing standard to follow, not a certification claim.

- Use short, active sentences and plain words.
- Keep instructions to about 20 words or fewer.
- Give one action per step.
- Explain each technical term once in plain words.
- Ask one question at a time.
- Give one step at a time.
- Say what happens next and why.
- Check that the member is comfortable before moving on.
- Never assume technical knowledge.
- Keep answers short unless the member asks for more detail.

## Work safely

- Read only records within the member's approved account and scope.
- Ask for explicit approval before sending messages, changing records, deleting data or rotating credentials.
- Show the proposed action and its target before asking for approval.
- Treat a command's presence as a capability, not permission to use it.
- Keep passwords, keys, sessions and customer records outside the repository.
- Never echo or log private environment values.
- Use synthetic records in examples and tests.
- Do not add company names, personal details, real record identifiers or private host paths to shared files.
- Report missing access or incomplete results. Do not replace unknown facts with guesses.
- Do not retry a failed write until its outcome is checked and the next action is approved.

## Preserve command contracts

- Keep API reads separate from website-session operations.
- Do not use `pm probe` as proof that website-session operations work.
- Preserve output redaction and nonzero failure exits.
- Preserve the manager-side completion refusal and the disabled force-pending-completion path.
- Preserve the opt-in `--complete` protocol and its refusal checks.
- Do not describe `--status open` as the whole unfinished board.
- Do not describe exhausted pages as a frozen snapshot of changing records.
- Treat legacy skill examples as examples. Current source and installed help determine available options.

## Make changes

1. Read the affected command and backend code.
2. Read the relevant tests before editing behavior.
3. Keep changes on a feature branch.
4. Add offline tests for changed behavior when code changes are authorized.
5. Use fake credentials and prevent test requests from reaching the service.
6. Check the documented wheel contract when packaging changes.
7. Report the changed behavior, checks run and remaining limits.

Do not run a live account test without separate approval.
Do not change account permissions, shared credentials or deployments as part of a documentation task.
Keep existing safeguards in the source and referenced documents.
