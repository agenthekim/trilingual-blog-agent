# 초기 환경 설정 가이드

**언어 선택**: [English](SETUP.md) · **한국어** · [日本語](ja-SETUP.md)

이 문서는 **Trilingual Blog Agent** 저장소를 처음 클론한 뒤 Google Cloud Vertex AI, 블로그 플랫폼 API 자격증명(DEV Community 및 Qiita), 그리고 Google Slides OAuth 연동을 설정하는 절차를 안내합니다.

> **참고**: 1단계(원본 수집), 2단계(3개 국어 번역·현지화), 4단계(마크다운 발표 원고 기획)는 Google Cloud Vertex AI만 설정되어 있으면 동작합니다. 블로그 자동 게시(DEV Community, Qiita)나 Google Drive 내 슬라이드 덱 자동 생성을 사용할 때만 해당 자격증명을 추가로 설정하면 됩니다.

---

<a id="1-google-cloud-and-vertex-ai"></a>
## 1. Google Cloud 및 Vertex AI 설정

1. [uv](https://docs.astral.sh/uv/), [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), 그리고 `google-agents-cli`를 설치합니다.
   ```bash
   uv tool install google-agents-cli
   agents-cli install
   ```
2. Application Default Credentials(ADC) 인증을 수행하고 GCP 프로젝트에서 Vertex AI 및 Secret Manager API를 활성화합니다.
   ```bash
   gcloud auth application-default login
   gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com
   ```
3. `.env.example` 파일을 `.env`로 복사하고 프로젝트 ID를 입력합니다.
   ```bash
   cp .env.example .env
   # .env 파일을 열어 GOOGLE_CLOUD_PROJECT=your-gcp-project-id 로 수정
   ```

---

<a id="2-dev-community-api-key"></a>
## 2. DEV Community API 키 발급 (영어 블로그)

영어 포스트는 Forem REST API를 통해 [DEV Community (dev.to)](https://dev.to)에 게시됩니다.

1. DEV Community에 로그인한 뒤 **Settings → Extensions** 페이지로 이동합니다.
   * `https://dev.to/settings/extensions`
2. 페이지 하단 **DEV Community API Keys** 섹션에서 설명(예: `trilingual-blog-agent`)을 입력하고 **Generate API Key**를 클릭합니다.
3. 발급된 키를 Google Cloud Secret Manager에 저장합니다.
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create DEVTO_API_KEY --data-file=- && unset TOKEN
   ```
   *(이미 `DEVTO_API_KEY` 시크릿이 존재한다면 `gcloud secrets versions add DEVTO_API_KEY --data-file=-` 명령을 사용하세요.)*

---

<a id="3-qiita-access-token"></a>
## 3. Qiita 액세스 토큰 발급 (일본어 블로그)

일본어 포스트는 Qiita v2 API를 통해 [Qiita](https://qiita.com)에 게시됩니다.

1. Qiita에 로그인한 뒤 **Settings → Applications** 토큰 발급 페이지로 이동합니다.
   * `https://qiita.com/settings/tokens/new`
2. 토큰 설명을 입력하고 **`read_qiita`** 및 **`write_qiita`** 권한에 체크한 뒤 **Generate token**을 클릭합니다.
3. 발급된 토큰을 Google Cloud Secret Manager에 저장합니다.
   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create QIITA_ACCESS_TOKEN --data-file=- && unset TOKEN
   ```
   *(이미 `QIITA_ACCESS_TOKEN` 시크릿이 존재한다면 `gcloud secrets versions add QIITA_ACCESS_TOKEN --data-file=-` 명령을 사용하세요.)*

---

<a id="4-google-slides-oauth"></a>
## 4. Google Slides OAuth 연동 설정

발표자 노트가 포함된 Google Slides 프레젠테이션을 본인의 Google Drive에 자동 생성하려면 최초 1회 OAuth 권한 부여가 필요합니다.

1. Google Cloud Console에서 **Google Slides API**와 **Google Drive API**를 활성화합니다.
   ```bash
   gcloud services enable slides.googleapis.com drive.googleapis.com
   ```
2. **OAuth 동의 화면(OAuth consent screen)**을 외부(External / Testing)로 구성하고, 슬라이드를 저장할 개인 Google 계정을 **테스트 사용자(Test users)**에 추가합니다.
3. **API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**에서 애플리케이션 유형을 **데스크톱 앱(Desktop app)**으로 선택하고 JSON 파일(예: `~/client_secret.json`)을 다운로드합니다.
4. 인증 헬퍼 스크립트를 실행하여 권한을 부여합니다.
   ```bash
   uv run python scripts/authorize_google_slides.py \
       --client-file ~/client_secret.json --account you@gmail.com
   ```
   승인이 완료되면 에이전트가 생성한 파일에만 접근하는 `drive.file` 범위의 리프레시 토큰이 Secret Manager의 `GOOGLE_SLIDES_OAUTH`에 안전하게 저장됩니다.

---

<a id="5-naver-blog-html-export"></a>
## 5. 네이버 블로그 HTML 내보내기 (한국어 블로그)

한국어 포스트가 게시되는 **네이버 블로그**는 외부 쓰기 API를 제공하지 않으므로 별도의 인증 키 설정이 필요하지 않습니다.

* 한국어 원고를 검토 완료(Finalize)하면 붙여넣기용 `naver_blog.html` 파일이 생성됩니다.
* `naver_blog.html`을 열어 렌더링된 본문을 네이버 스마트에디터 ONE에 붙여넣어 발행한 뒤, 발행된 URL을 웹 UI에 등록하면 됩니다.

---

<a id="6-starting-the-web-ui"></a>
## 6. 웹 UI 실행 및 연동 상태 확인

설정을 마친 뒤 웹 서버를 실행하고 우측 상단의 **설정(Settings)** 버튼을 눌러 각 플랫폼이 `✓ 연결됨`으로 표시되는지 확인합니다.

```bash
uv run python -m app.web_ui --host 127.0.0.1 --port 8502
```
