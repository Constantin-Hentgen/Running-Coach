<p align="center">
  <img src="assets/bob.png" alt="Bob, the trail-running robot" width="160">
</p>

<h1 align="center">Running Coach</h1>

<p align="center">
  A self-hosted AI running coach on Telegram: plans your sessions in your own calendar, knows your training data and the weather, tracks what you eat, and learns your preferences as you chat.
</p>

<p align="center">
  <a href="https://github.com/Constantin-Hentgen/Running-Coach/actions/workflows/ci.yml"><img src="https://github.com/Constantin-Hentgen/Running-Coach/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/Constantin-Hentgen/Running-Coach/releases/latest"><img src="https://img.shields.io/github/v/release/Constantin-Hentgen/Running-Coach" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Constantin-Hentgen/Running-Coach" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+">
</p>

```text
You ▸ Décale ma sortie longue de samedi à dimanche matin, il va pleuvoir
Bob ▸ C'est calé dimanche 9h, il fera sec 👌
Bob ▸ Vas-y tranquille, t'as encore de la fatigue dans les jambes 😮‍💨
```

> [!NOTE]
> The coach speaks **French**. This documentation is in English.

## Features

- 📅 **Plans in your CalDAV calendar** (Radicale), backward from your goals; every write is checked on the server
- 🏃 **Uses your Dreeve data**: load, whole run history, a debrief and check-in after each run
- 🌧️ **Weather alerts** per session and training place
- 🥗 **Meals and nutrition**: meal ideas, shopping checklist, stock, food log computed from the Ciqual table
- 🗺️ **Running loops** around home or work, as maps and GPX files
- 🎙️ **Voice notes** transcribed locally, 🧠 **memory** of your preferences, single-user and self-hosted

## Installation

Requirements: Docker with Compose, an [Anthropic](https://console.anthropic.com/settings/keys) API key, a Telegram bot from [@BotFather](https://t.me/BotFather) and a CalDAV calendar dedicated to running. See [Compatibility](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Compatibility).

1. Get the code and create your configuration:
   ```bash
   git clone https://github.com/Constantin-Hentgen/Running-Coach.git && cd Running-Coach
   cp .env.example .env
   ```
2. Fill in `.env`: at least `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `RADICALE_URL`, `RADICALE_USER`, `RADICALE_PASS` ([all options](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Configuration)).
3. Build and start it:
   ```bash
   docker compose up -d --build
   ```

## Quickstart

Message your bot: while `TELEGRAM_CHAT_ID` is empty it only replies with your chat ID. Put that ID in `.env`, run `docker compose up -d` again, then introduce yourself to the coach (« Semi de Paris le 7 mars, je vise 1h45 ») and try `/help`. Details in [Getting Started](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Getting-Started).

## Documentation

The **[wiki](https://github.com/Constantin-Hentgen/Running-Coach/wiki)** covers everything after installation:

- [Configuration](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Configuration): every environment variable, with its type and default
- [Deployment](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Deployment): Docker Compose, Portainer, updates and backups
- [Commands](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Commands) and [Coaching](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Coaching): what you can ask and what the coach does on its own
- [Weather Alerts](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Weather-Alerts), [Meals and Nutrition](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Meals-and-Nutrition), [Running Routes](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Running-Routes)
- [Error Messages](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Error-Messages) and [Known Limitations](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Known-Limitations): when something goes wrong
- [How It Works](https://github.com/Constantin-Hentgen/Running-Coach/wiki/How-It-Works), [Integrations](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Integrations), [Privacy and Safeguards](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Privacy-and-Safeguards), [Contributing](https://github.com/Constantin-Hentgen/Running-Coach/wiki/Contributing)

Release notes: [CHANGELOG.md](CHANGELOG.md) and [Releases](https://github.com/Constantin-Hentgen/Running-Coach/releases).

## License

[MIT](LICENSE). Nutrition data: Anses. 2020. Table de composition nutritionnelle des aliments Ciqual, [Licence Ouverte](https://www.etalab.gouv.fr/licence-ouverte-open-licence). Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
