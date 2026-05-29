# Agent Operating Notes

This repo builds a Sarathi DL renewal browser agent. The success criterion is not
"the script ran"; success means the agent completes the application flow
automatically, or stops with a clear human question when the portal needs data or
judgment it cannot infer.

## Current Priorities

- Keep the user in the loop. Do not make irreversible assumptions when stuck.
- Before committing or pushing, run a local verification and inspect the logs.
- Prefer small fixes that directly improve the browser agent's reliability.
- Avoid broad refactors while the portal flow is still being stabilized.

## Agent Behavior Rules

- Observe the real page: screenshot, page text, DOM selectors, current input
  values, selected dropdown values, and checkbox states.
- Batch-fill ordinary known fields with `fill_many` when possible.
- Use `check` for checkboxes/radios; do not JS-click disabled buttons.
- If the page asks for missing customer data, use `human_help` with
  `tool_args.field_key` so the answer is stored in `job.customer_data`.
- If the same page state repeats, treat no-op fills/scrolls/waits as failures.
  The agent must try a different approach or ask for human input.
- Portal validation alerts are failures unless explicitly classified as a valid
  branch, such as "Application already exists..." after confirming DL details.

## Known Test Data

- Present address PIN code for the test customer: `334401`
- Optional email, only if required by the portal: `sipanijai@gmail.com`

## Git Policy

- Do not push to GitHub before local testing proves the change.
- Keep commits focused and describe the observed failure that the commit fixes.
