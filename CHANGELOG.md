# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.2.0](https://github.com/Constantin-Hentgen/Running-Coach/compare/v3.1.0...v3.2.0) (2026-11-14)


### Added

* **i18n:** configurable bot language (BOT_LANGUAGE: fr, en) ([#48](https://github.com/Constantin-Hentgen/Running-Coach/issues/48)) ([e9608f2](https://github.com/Constantin-Hentgen/Running-Coach/commit/e9608f2c0374b1a31c6a658d870edbff2febe849))


### Fixed

* **nutrition:** reliable food log and a daily 22:00 recap ([#46](https://github.com/Constantin-Hentgen/Running-Coach/issues/46)) ([dac8559](https://github.com/Constantin-Hentgen/Running-Coach/commit/dac855904165e02324bdd99bdaa8f14a49a7dba0))

## [3.1.0](https://github.com/Constantin-Hentgen/Running-Coach/compare/v3.0.0...v3.1.0) (2026-11-14)


### Added

* **routes:** /newroute options for distance, surface and place ([be52369](https://github.com/Constantin-Hentgen/Running-Coach/commit/be5236949a3ee3e8d0cd133008a83b05df05fe51)), closes [#40](https://github.com/Constantin-Hentgen/Running-Coach/issues/40)
* **weather:** reason hour by hour on each session's exact slot ([cf23ca9](https://github.com/Constantin-Hentgen/Running-Coach/commit/cf23ca94f4bb85b2070ad60e08ca0656c1deb14e)), closes [#41](https://github.com/Constantin-Hentgen/Running-Coach/issues/41)

## [3.0.0] - 2026-11-14

### Removed
- The `/memoire` command: use `/memory` (BREAKING).
- The `/nouveau` command.
- Python 3.11 support: Python 3.12 or newer is required (BREAKING for installs outside Docker; the Docker image uses 3.13).

### Added
- Telegram commands with one-word English names, shown in Telegram's `/` menu: `/plan`, `/load`, `/runs`, `/weather`, `/goals`, `/memory`, `/debrief`, `/review`, `/briefing`, `/help`.
- Structured goals with countdown and periodization built backward from the main goal (`/goals`).
- Proposal buttons: changes the coach suggests arrive in their own message with « Oui, vas-y / Modifier / Non ».
- Post-run check-in (perceived effort and legs), debrief after every new Dreeve run, weekly review (`/review`) on the planning model.
- Weather alerts per session and training place (rain, heat, wind, ice) with an « Adapter » button and an all-clear message.
- Voice messages transcribed locally with faster-whisper.
- Memory ordered by priority (rules, strong preferences, hints) and a longer conversation history.
- Goal-driven shopping list (`/shopping`) as a Telegram checklist, optionally saved and kept in sync in a separate shopping calendar; meal-prep recipes (`/recipes`); current list (`/list`).
- Kitchen stock (`/stock`) with keep-by dates and « Encore là ? » checks; meal buttons that update the stock.
- `/eat`: three dishes for the meal coming now (quick, relaxed, indulgent), recipe sent on choice, grounded in the open Wikibooks cookbook; automatic dish ideas before meals; no shopping suggestions after `SHOP_LAST_CALL`.
- Food log: « tu as mangé quoi ? » questions with one-tap guesses; nutrients computed from the bundled ANSES Ciqual table; energy balance from a profile and Dreeve run calories (`/nutrition`); evening nutrition check; weekly food variety and a dish of the week.
- The whole running history from Dreeve in the coach's context.
- Coach persona "Bob", configurable with `BOT_NAME`, with a profile picture; short chat-style messages with typing pauses; occasional GIFs and `/meme` (GIPHY).
- Running loops around home or work as maps and GPX files (`/routes`, `/newroute` for a loop never run), via BRouter and OpenStreetMap.
- `/health` reports the state of new-run detection.

### Changed
- The daily briefing runs on the conversation model; only the weekly review uses `CLAUDE_PLANNING_MODEL`.
- The morning note only covers today; calendar changes are only proposed for the next two days.
- Messages read like a chat: no log-style reports, failures stated in one plain sentence.
- Messages sent in a row are answered together.
- The evening nutrition check defaults to 22:30.
- `bot.py` split into focused handler modules (no behaviour change).

### Fixed
- Every proactive message (debriefs, briefing, alerts, check-ins) failed because of the chat ID being passed twice.
- A run's recap is no longer lost when the debrief fails or hangs.
- A failed memory update no longer deletes the memory it was replacing; saves are flushed to disk and a corrupt state file is kept with a timestamp.
- A stalled model or Dreeve call can no longer freeze the bot (timeouts on every call and per message).
- False "nothing changed in your calendar" warnings on stock and meal replies.
- The GIPHY API key could leak into logs and model context through error messages.
- Tool calls written as text by the model no longer reach Telegram.
- The coach answers the question instead of dodging it.
- Proposal buttons: answering one no longer clears the others.
- GIF session check compared UTC and local times as text.
- The Ciqual table was missing from the repository because of a `.gitignore` rule.

### Security
- FastAPI `/docs`, `/redoc` and `/openapi.json` are disabled.
- Text from tools and fetched pages is treated as data, never instructions; read-only runs can't delete memories or goals.

## [2.0.0] - 2026-11-08

### Added
- Self-hosted running coach on Telegram: Claude plans sessions in a CalDAV calendar using Dreeve training data and the weather, and remembers preferences.

### Fixed
- Portainer Git stacks: environment files are optional and every variable is passed through explicit `${VAR}` interpolation.

[3.0.0]: https://github.com/Constantin-Hentgen/Running-Coach/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/Constantin-Hentgen/Running-Coach/releases/tag/v2.0.0
