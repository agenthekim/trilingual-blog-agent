# Resources specific to the trilingual tech-blog agent, added on top of the
# scaffolded Agent Runtime deployment in this directory.
#
#   - Firestore: persistent store for article revisions and publications.
#   - Secret Manager: containers for the platform credentials. Terraform creates
#     the secrets but never their values; add a version by hand so that no
#     credential ever passes through Terraform state or this repository:
#       printf '%s' "$TOKEN" | gcloud secrets versions add QIITA_ACCESS_TOKEN --data-file=-
#   - Cloud DLP: optional PII redaction for logs and memory.
#   - IAM: the agent's own identity gets only the roles these features need.

locals {
  blog_agent_services = [
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
  ]

  blog_agent_secret_ids = [
    "QIITA_ACCESS_TOKEN",
    "DEVTO_API_KEY",
    "GOOGLE_SLIDES_OAUTH",
  ]

  agent_principal = "principal://${google_vertex_ai_reasoning_engine.app.spec[0].effective_identity}"
}

variable "create_firestore_database" {
  type        = bool
  description = "Create the (default) Firestore database. Leave false if the project already has one."
  default     = false
}

variable "firestore_location" {
  type        = string
  description = "Location of the Firestore database when it is created here."
  default     = "us-central1"
}

resource "google_project_service" "blog_agent_services" {
  for_each = toset(local.blog_agent_services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false

  depends_on = [google_project_service.bootstrap]
}

resource "google_firestore_database" "articles" {
  count = var.create_firestore_database ? 1 : 0

  project     = var.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.blog_agent_services]
}

resource "google_secret_manager_secret" "platform_credentials" {
  for_each = toset(local.blog_agent_secret_ids)

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.blog_agent_services]
}

# Least privilege: the agent may read these three secrets and nothing else.
resource "google_secret_manager_secret_iam_member" "agent_reads_platform_credentials" {
  for_each = google_secret_manager_secret.platform_credentials

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.agent_principal
}

resource "google_project_iam_member" "agent_firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.agent_principal

  depends_on = [google_project_service.blog_agent_services]
}

resource "google_project_iam_member" "agent_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = local.agent_principal

  depends_on = [google_project_service.blog_agent_services]
}
