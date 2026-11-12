# Setup Guide

This guide walks through configuring **Trilingual Blog Agent** from a fresh clone, including Google Cloud Vertex AI, publishing platform credentials (DEV Community and Qiita), and Google Slides OAuth integration.

> **Note**: Stages 1 (Source intake), 2 (Trilingual localization), and 4 (Slide planning in Markdown) work with only Google Cloud Vertex AI configured. Platform credentials and Google Slides OAuth are only needed when you want to publish drafts through the APIs or build Google Slides decks in Google Drive.

---

## 1. Google Cloud and Vertex AI

1. Install [uv](https://docs.astral.sh/uv/), the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), and `google-agents-cli`:
   ```bash
   uv tool install google-agents-cli
   agents-cli install
   ```
2. Authenticate with Application Default Credentials and enable Vertex AI and Secret Manager on your project:
   ```bash
   gcloud auth application-default login
   gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com
   ```
3. Copy `.env.example` to `.env` and set your project ID:
   ```bash
   cp .env.example .env
   # Edit .env and set GOOGLE_CLOUD_PROJECT=your-gcp-project-id
   ```

---

## 2. DEV Community API Key

English posts are published to [DEV Community (dev.to)](https://dev.to) via the Forem REST API.

1. Sign in to DEV Community and open **Settings → Extensions**:
   * `https://dev.to/settings/extensions`
2. Under **DEV Community API Keys**, enter a short description (for example `trilingual-blog-agent`) and click **Generate API Key**.
3. Store the key in Google Cloud Secret Manager (recommended) or in your local environment:
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create DEVTO_API_KEY --data-file=- && unset TOKEN
   ```
   *(If `DEVTO_API_KEY` already exists, use `gcloud secrets versions add DEVTO_API_KEY --data-file=-`.)*

---

## 3. Qiita Access Token

Japanese posts are published to [Qiita](https://qiita.com) via the Qiita v2 API.

1. Sign in to Qiita and open **Settings → Applications**:
   * `https://qiita.com/settings/tokens/new`
2. Enter a description, check **`read_qiita`** and **`write_qiita`**, and click **Generate token**.
3. Store the token in Google Cloud Secret Manager:
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create QIITA_ACCESS_TOKEN --data-file=- && unset TOKEN
   ```
   *(If `QIITA_ACCESS_TOKEN` already exists, use `gcloud secrets versions add QIITA_ACCESS_TOKEN --data-file=-`.)*

---

## 4. Google Slides OAuth

To create presentation decks with speaker notes directly in your Google Drive:

1. In the Google Cloud Console, enable the **Google Slides API** and **Google Drive API**:
   ```bash
   gcloud services enable slides.googleapis.com drive.googleapis.com
   ```
2. Configure the **OAuth consent screen** (External / Testing) and add your personal Google account under **Test users**.
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**, choose **Desktop app**, and download the JSON file (for example `~/client_secret.json`).
4. Run the one-time authorization helper:
   ```bash
   uv run python scripts/authorize_google_slides.py \
       --client-file ~/client_secret.json --account you@gmail.com
   ```
   This stores the refresh token in Secret Manager under `GOOGLE_SLIDES_OAUTH` with `drive.file` scope (accessing only files created by this agent).

---

## 5. Naver Blog HTML Export

Korean posts target **Naver Blog**, which does not provide a public write API:

* No credential is required.
* When you finalize the Korean post, the agent generates a paste-ready `naver_blog.html` file.
* Open `naver_blog.html`, copy the rendered content into the Naver SmartEditor ONE composer, publish, and register the published URL in the web UI.

---

## 6. Starting the Web UI

Once configured, start the web server and open **Settings** in the top-right corner to verify that all integrations show as connected:

```bash
uv run python -m app.web_ui --host 127.0.0.1 --port 8502
```
