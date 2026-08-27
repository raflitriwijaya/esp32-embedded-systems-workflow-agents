# The Compile Loop

`PostToolUse` and `Stop`. The first states a fact; the second, and only the second, can hold a turn.

---

## 1. Why Section 3 needs something the earlier sections did not

Sections 1 and 2 govern state and documents. The framework's job there is to stop the agent asserting what it cannot show.

Section 3 governs **code**, and that changes the premise. For the first time the artifact is executable, an oracle exists that the agent does not control, and running it costs one command. The job shifts from *prevent unevidenced claims* to **make sure the oracle was actually consulted before the claim was made**.

---

## 2. `PostToolUse` — the half that carries the load

After a write to a file the ESP-IDF build consumes, the hook states the consequence:

```
[stage-kernel] main.c is a build input. The archived build log no longer
establishes that this tree compiles:
  esp32s3: STALE - main/main.c was modified 4m after this log was written...
The Gate 2->3 zero-warning criterion reads UNVERIFIABLE until a build is
archived.
```

It cannot block — `PostToolUse` fires after the tool already ran — and that is right. The point is that the model **knows**, not that the write is prevented. No loop is possible and it fails open on every error.

It stays silent on files the firmware build does not consume. A host unit test under `tests/` is a `.c` file and is not a build input; a README is not either.

---

## 3. `Stop` — the only fail-closed mechanism here

Every other hook in this framework fails open. A crashed guard allows the write. An unreadable log reports *not checked*. This one can hold the engineer's turn, so it is built differently.

### 3.1 It holds only on a conjunction

Both halves must be true:

| | |
|---|---|
| **Factual** | the archived build evidence is stale, no-op, or absent for a configured target |
| **Linguistic** | `last_assistant_message` asserts a build or test result **as fact** |

Either alone is not enough. The factual half alone would fire on every edit — an engineer edits twenty times before building once. The linguistic half alone would fire on an honest report of a real build.

### 3.2 The claim matcher is deliberately narrow

A false positive here holds a turn hostage, which is the fastest way to get the whole mechanism switched off. So a sentence must assert a result *and* carry no hedge.

Tuned against 28 worked cases, currently at zero errors. The ones that shaped it:

| Sentence | Holds? | Why |
|---|---|---|
| `The code compiles without errors and I moved on.` | yes | assertion |
| `Zero warnings across every configured target.` | yes | assertion |
| `The archived log shows zero warnings, but it is stale.` | **no** | citing evidence, not asserting |
| `According to tests/reports/build-esp32s3.log there are zero warnings.` | **no** | citing evidence |
| `Fix the error, then the build passes.` | **no** | a sequence, not a state |
| `I cannot say whether it compiles cleanly without building.` | **no** | explicit refusal to claim |

Two of those exposed real bugs. `error` had been a hedge word, which silently suppressed *"compiles without errors"* — an assertion. And the sentence splitter broke on the period inside `build-esp32s3.log`, cutting the citation in half and leaving the remainder looking like a bare claim.

### 3.3 Loop protection is mine to build

The documented `Stop` contract carries **no loop protection and no `stop_hook_active` field**. A hook that holds a turn the model cannot satisfy would spin. So the limits are load-bearing, not caution:

- **never twice in a row.** A second consecutive hold means the model could not satisfy the first, and holding again helps nobody. It says so and lets the turn end.
- **never more than 3 times per session.** Tracked per `session_id` under the OS temp directory, not in the project.
- **`STAGE_KERNEL_NO_STOP=1`** disables it entirely.
- **`.no-stage-governance`** in the project root disables it, as it does everything else here.
- **any error at all fails open.** A hold caused by the hook itself breaking would be indefensible.

### 3.4 What the hold says

```
This turn states a build or test result as fact, and the archived evidence
does not establish it:
  claimed: "It compiles cleanly."
  esp32s3: STALE - main/main.c was modified 0s after this log was written...

Run the build and let it answer - tools/idf_run.ps1 -Target <target> build
archives a log, or use the esp-idf MCP build_project tool. If you would
rather not, say plainly that the build state is unverified and stop.
(hold 1 of 3 this session; STAGE_KERNEL_NO_STOP=1 disables this entirely)
```

The second option is not a loophole. Saying *the build state is unverified* is a true statement, and the framework prefers it to a false one. The hold exists to make the choice explicit, not to force a build.

Exit code 2 is what holds the turn; the stderr text is what Claude is shown. `2>&1` inside PowerShell wraps native stderr in a `NativeCommandError`, which would put a stack trace in the middle of the explanation — so the script captures to a file and relays it verbatim.

---

## 4. Registration

```json
"PostToolUse": [
  { "matcher": "Write|Edit",
    "hooks": [ { "type": "command",
      "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\post_tool_use_build.ps1\"" } ] }
],
"Stop": [
  { "hooks": [ { "type": "command",
      "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\stop_build_claim.ps1\"" } ] }
]
```

Both scripts bail out on a cheap substring test before starting Python, so a turn with no build language and a write with no build input cost nothing.

---

## 5. What this does not establish

That the build is correct. That the tests are meaningful. That a passing build means the firmware works.

It establishes one thing: **that a claim about the build was made while the evidence for it existed.** Everything past that is the engineer's.
