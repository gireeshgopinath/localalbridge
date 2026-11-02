# Local workers for Codex

Codex plans tasks, evaluates evidence, and makes acceptance decisions. OpenCode runs implementation and repair jobs on **LM Studio / qwen3.8-27b**. A local Python MCP bridge manages the queue, worktrees, checks, and records. No OpenAI API key is required by this bridge.

## Start on this machine

The bridge is registered as `local-workers` after setup. Restart Codex or open a new chat to load its five tools. Keep LM Studio's server running on `127.0.0.1:1234`. On the measured RTX 5080 Laptop 16GB machine, load the tuned profile using Node.js 22+:

```powershell
npm ci --prefix tuning --ignore-scripts
npm run load --prefix tuning
```

This retains **16,384 context tokens**, one inference slot, and the existing Qwen weights. See [PERFORMANCE.md](PERFORMANCE.md) for measured timings, hardware requirements, the automatic-placement alternative, and restoration. Reapply this command after unloading the model or restarting LM Studio; it does not edit LM Studio's global defaults.

For standard automatic placement without the tuned profile:

```powershell
lms load qwen3.8-27b --context-length 16384 --parallel 1 --identifier qwen3.8-27b --yes
```

In Codex, use a request such as:

> Use local-workers for this clean Git repository. Explore locally, define small independent tasks, and delegate all edits and repairs to the local worker. Review each batch's full required artifacts, then review the integrated result before acceptance. Keep routine logs local.

The model must already be loaded. An unavailable model blocks the job; there is no cloud worker fallback.

## Install or register again

Requires Windows, Python 3.12+, Git, OpenCode 2.x, LM Studio, and the downloaded Qwen model. This build was verified with OpenCode 2.0.18; rerun the tests and pilot after upgrades. From this directory:

```powershell
.\setup.ps1 -Register -Doctor -Test
```

This creates `.venv`, installs the pinned requirements, checks the local tool call, runs tests, and registers the MCP server. It preserves existing OpenCode global settings and other MCP servers. To use an existing Python environment containing the dependencies:

```powershell
.\setup.ps1 -PythonPath 'C:\path\to\python.exe' -StatePath 'C:\short\worker-state' -Register -Doctor
```

Use a short state directory outside the target repository on Windows. Git for Windows has limits on some worktree metadata paths even with `core.longpaths=true`. A second bridge instance cannot own the same state directory. Each client needs a separate state directory or must connect through the same client session.

## The five tools

| Tool | Use |
|---|---|
| `submit_batch(repo_path, tasks)` | Queue 1–3 approved independent tasks; return an ID immediately. |
| `get_batch(batch_id)` | Read compact status, required artifact IDs, and usage. |
| `read_artifact(batch_id, artifact_id, cursor=0)` | Read evidence in pages; follow `next_cursor` until null. |
| `decide_batch(batch_id, action, feedback='')` | Main orchestrator records `approve`, `revise`, or `stop`. |
| `cancel_batch(batch_id)` | Stop owned execution and retain evidence. |

An exploration task uses `role: "explore"`, empty `allowed_files` and `verification_commands`, and concrete acceptance criteria. An implementation task specifies 1–3 exact relative filenames and approved verification commands as argv lists. See `example-batch.json`. Dependencies and overlapping filenames within a batch are rejected.

The original repository must be clean, have a committed HEAD, and be supplied at its root. All jobs start from its captured revision. The original checkout stays unchanged. Task worktrees and the integrated result are retained under the state directory.

## Review workflow

1. Submit a local exploration task. Inspect its report and approve it when its criteria are met.
2. Define up to three small, independent implementation tasks with explicit criteria and checks. Submit the batch.
3. At `needs_review`, read **every page** of each required patch, full-file context, check record, and report. Fetch raw logs or additional repository context when needed. Evaluate correctness yourself.
4. `approve` starts integration in a separate worktree. A local review session assesses the captured source, and the bridge independently reruns the approved checks.
5. At `final_review`, read all required integrated artifacts. A second `approve` records completion. Tests alone never accept work.

Read receipts enforce evidence access before approval; they cannot prove the quality of a model's judgment. Local reviews supply findings for Codex; they never grant acceptance. Implementation sessions have local file tools. Review sessions receive captured source and criteria with tools disabled, which keeps them focused and prevents edits.

To request repairs, use `revise` with concrete feedback. Each task has an initial attempt plus two repairs total. Exhausted budgets or scope changes require a new batch. Restarted queued/running/integrating jobs become `blocked` and require an explicit revision; a still-active recorded process prevents resumption. Cancellation cannot transition to completion.

Each attempt shares a 20-minute/30-observed-tool-call budget across implementation and review. The CLI event monitor stops execution at the tool boundary; operations already in flight may finish before cancellation. Tool permissions are not an OS sandbox. Approved verification commands execute trusted local project code with the current user's authority. Python aliases use the bridge interpreter; give an explicit project interpreter path when checks need project dependencies.

Captured review source is limited to 32,000 characters per task/batch for the 16K local context. Split larger tasks. Full patches and context remain available to Codex; evidence is never silently shortened for acceptance. Artifact reads redact common credential formats, but credential detection is not complete. Do not include secrets in tasks or review files.

The bridge leaves accepted changes in the integration worktree and patch. It does not commit, push, deploy, or automatically apply changes to the original checkout. To build a later batch on these changes, first review/apply/commit the result through a separately requested action and provide a clean baseline.

## Tests and repeatable pilot

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe pilot.py prepare --root 'C:\short\new-pilot'
```

Pilot preparation refuses existing directories and creates only a new synthetic Git fixture, its initial fixture commit, immutable check oracle, and four batch definitions. Submit the ten tasks from `batches.json` through the normal MCP workflow and review both gates. There is deliberately no automatic acceptance script.

```powershell
.\.venv\Scripts\python.exe pilot.py report --state 'C:\short\pilot-state'
```

The report counts recorded acceptance decisions. Read `VALIDATION.md` for the measured results of this build. Cloud token usage is reported as unknown; CLI step-token totals can be incomplete, so the bridge does not fabricate savings percentages. Status packets target roughly 1,500 tokens; full logs stay local. Review-ready and blocked states emit compact MCP log notifications to the connected client. Client support varies, so Codex may still need bounded status polling. Codex must remain active to review jobs; the bridge does not wake an idle chat or create scheduled automations.

## References

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [OpenCode local models](https://dev.opencode.ai/v2/docs/models/)
- [OpenCode policies](https://dev.opencode.ai/v2/docs/policies/)
- [LM Studio tool use](https://lmstudio.ai/docs/developer/openai-compat/tools)
