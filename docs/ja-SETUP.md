# セットアップガイド

**言語切替**: [English](SETUP.md) · [한국어](kr-SETUP.md) · **日本語**

このガイドでは、**Trilingual Blog Agent** リポジトリをクローンした後に、Google Cloud Vertex AI、各ブログプラットフォームのAPI認証情報（DEV Community および Qiita）、Google Slides OAuth 連携を設定する手順を説明します。

> **注記**: ステージ1（元記事の取得）、ステージ2（3言語への翻訳・ローカライズ）、ステージ4（Markdownでの発表スライド構成案の作成）は、Google Cloud Vertex AI のみの設定で動作します。API経由でのブログ下書き・公開投稿や、Google Drive へのスライドデッキ自動作成を行う場合のみ、以下の認証情報を追加設定してください。

---

<a id="1-google-cloud-and-vertex-ai"></a>
## 1. Google Cloud と Vertex AI の設定

1. [uv](https://docs.astral.sh/uv/)、[Google Cloud SDK](https://cloud.google.com/sdk/docs/install)、および `google-agents-cli` をインストールします。
   ```bash
   uv tool install google-agents-cli
   agents-cli install
   ```
2. Application Default Credentials（ADC）で認証し、対象プロジェクトで Vertex AI と Secret Manager API を有効化します。
   ```bash
   gcloud auth application-default login
   gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com
   ```
3. `.env.example` を `.env` にコピーし、プロジェクトIDを設定します。
   ```bash
   cp .env.example .env
   # .env を開き、GOOGLE_CLOUD_PROJECT=your-gcp-project-id を設定
   ```

---

<a id="2-dev-community-api-key"></a>
## 2. DEV Community API キーの取得（英語ブログ）

英語の記事は Forem REST API を通じて [DEV Community (dev.to)](https://dev.to) に投稿されます。

1. DEV Community にログインし、**Settings → Extensions** を開きます。
   * `https://dev.to/settings/extensions`
2. **DEV Community API Keys** セクションで説明（例: `trilingual-blog-agent`）を入力し、**Generate API Key** をクリックします。
3. 発行されたキーを Google Cloud Secret Manager に保存します。
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create DEVTO_API_KEY --data-file=- && unset TOKEN
   ```
   *(すでに `DEVTO_API_KEY` が存在する場合は `gcloud secrets versions add DEVTO_API_KEY --data-file=-` を使用してください。)*

---

<a id="3-qiita-access-token"></a>
## 3. Qiita アクセストークンの取得（日本語ブログ）

日本語の記事は Qiita v2 API を通じて [Qiita](https://qiita.com) に投稿されます。

1. Qiita にログインし、**設定 → アプリケーション** のトークン発行画面を開きます。
   * `https://qiita.com/settings/tokens/new`
2. トークンの説明を入力し、**`read_qiita`** と **`write_qiita`** にチェックを入れて **発行する** をクリックします。
3. 発行されたトークンを Google Cloud Secret Manager に保存します。
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create QIITA_ACCESS_TOKEN --data-file=- && unset TOKEN
   ```
   *(すでに `QIITA_ACCESS_TOKEN` が存在する場合は `gcloud secrets versions add QIITA_ACCESS_TOKEN --data-file=-` を使用してください。)*

---

<a id="4-google-slides-oauth"></a>
## 4. Google Slides OAuth 連携の設定

発表者ノート付きの Google Slides プレゼンテーションを Google Drive に自動作成するには、初回のみ OAuth 認可が必要です。

1. Google Cloud Console で **Google Slides API** と **Google Drive API** を有効化します。
   ```bash
   gcloud services enable slides.googleapis.com drive.googleapis.com
   ```
2. **OAuth 同意画面（OAuth consent screen）** を外部（External / Testing）として設定し、個人の Google アカウントを **テストユーザー（Test users）** に追加します。
3. **APIとサービス → 認証情報 → 認証情報を作成 → OAuth クライアント ID** でアプリケーションの種類に **デスクトップ アプリ（Desktop app）** を選択し、JSON ファイル（例: `~/client_secret.json`）をダウンロードします。
4. 認可ヘルパースクリプトを実行してアクセスを許可します。
   ```bash
   uv run python scripts/authorize_google_slides.py \
       --client-file ~/client_secret.json --account you@gmail.com
   ```
   承認が完了すると、エージェントが作成したファイルのみにアクセスできる `drive.file` スコープのリフレッシュトークンが Secret Manager の `GOOGLE_SLIDES_OAUTH` に保存されます。

---

<a id="5-naver-blog-html-export"></a>
## 5. Naver Blog HTML 出力（韓国語ブログ）

韓国語の記事の投稿先である **Naver Blog** には公開用の書き込みAPIがないため、APIキーの設定は不要です。

* 韓国語の記事を確定（Finalize）すると、貼り付け用の `naver_blog.html` ファイルが生成されます。
* `naver_blog.html` を開いて内容を Naver SmartEditor ONE に貼り付けて公開し、公開されたURLをWeb UIに登録します。

---

<a id="6-starting-the-web-ui"></a>
## 6. Web UI の起動と連携確認

設定が完了したら Web サーバーを起動し、右上の **設定（Settings）** ボタンからすべてのサービスが `✓ 連携済み` になっていることを確認してください。

```bash
uv run python -m app.web_ui --host 127.0.0.1 --port 8502
```
