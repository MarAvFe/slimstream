# slimstream

An automated pipeline that keeps phone photos/videos out of your Google storage quota by compressing them and routing everything through Mega, with a manifest that makes deletion of originals provably safe.

## The problem

Phones capture at "high quality" or "medium" by default — far more resolution than most day-to-day footage (a kid playing, a moment worth keeping) actually needs. That default quietly fills a Google One plan. The fix isn't "buy more storage," it's "stop paying to store quality you didn't ask for," with true high-quality capture remaining an explicit, opt-in exception rather than the everyday default.

## The opinion

A few calls this project makes on purpose, because they shape everything downstream:

- **Mega is the source of truth, not Google.** Google Photos has no bulk read API since March 2025 — you can't programmatically list or fetch your own library through it. Mega has real CLI/API control. So Google is upstream (where the phone dumps captures, unavoidably) and, optionally, downstream (Stage 2 republishing) — never a dependency in the middle of the pipeline.
- **The manifest is the product.** Transcoding is a one-line ffmpeg/ImageMagick call — not interesting. What's load-bearing is the state machine that makes the worker idempotent, resumable, and *structurally incapable* of deleting an original before its compressed replacement is verified. It's also the interface other people build against when they run this on their own Mega/VM/thresholds.
- **Reversibility over cleverness.** Every delete goes to Mega's Rubbish bin, not straight deletion. `--dry-run` is mandatory on first deployment. Nothing destructive runs unattended until it's been eyeballed on a small real sample.
- **Assumptions about vendor behavior get tested, not reasoned about.** Things like "does `mega-rm` actually go to Rubbish?" are empirical questions with cheap real-world tests — the design doesn't get to lean on an assumption until that test has run. See the Assumptions Register in [PROJECT_SPEC.md](PROJECT_SPEC.md).
- **Worker before control surface.** The headless pipeline must work with zero UI. A Telegram bot, if it ever exists, is a thin read-only skin over manifest state — it invents no logic of its own.

## How it works, in short

```
Phone → Google Photos (capture, unavoidable) → Mega (camera-upload mirror, source of truth)
                                                   │
                                          slimstream worker (headless, cloud VM)
                                            Job A: compress new arrivals
                                            Job B: rolling 30-day delete of originals
                                                   │
                                                manifest (state store)
```

Every original is tracked in a manifest keyed by content hash, moving through a state machine:

```
discovered → compressing → compressed → uploaded → verified → original_deleted
```

The one non-negotiable rule: an original can only be deleted from the `verified` state — never before. Job A (compress) and Job B (delete) are fully independent, so a bug in one can't cause a bad outcome in the other.

## Summarized build order

1. **Run the assumption tests first.** Especially: does `mega-rm` really move files to Rubbish (recoverable) and not delete them permanently? This is the single fact the whole safety model depends on — verify it before writing any pipeline code.
2. **Build the manifest module** — schema, state machine, query interface. Everything else is built to honor this.
3. **Build the two external edges**: `mega_client` (wraps MEGAcmd: list/get/put/rm-to-rubbish) and `transcoder` (ffmpeg/ImageMagick wrappers).
4. **Build Job A** (compress new arrivals). Run with `--dry-run` against a small real folder first, check transcode quality by hand.
5. **Build Job B** (rolling retention delete). `--dry-run` first, then live — only once Job A is trusted.
6. **MVP is done here.** Everything past this point — publishing compressed copies back to Google Photos, a Telegram control bot — is explicitly out of scope until Job A and Job B have run unattended, correctly, on real data.

Full design rationale, schema, transcode parameters, failure modes, and open decisions live in [PROJECT_SPEC.md](PROJECT_SPEC.md).
