# gcp-devops-takehome

Two-service app on Cloud Run: a public frontend (`service-a`) that calls a
private, internal-only backend (`service-b`) over the VPC with an
authenticated ID token. The backend reads a secret from Secret Manager at
runtime.

```
public internet
      │
      ▼
┌────────────────────┐
│ frontend (public)   │  Cloud Run, ingress: all
│ service-a           │  Direct VPC egress (all-traffic)
└─────────┬────────────┘
          │ private call over VPC
          │ (Authorization: Bearer <ID token>)
          ▼
┌────────────────────┐
│ backend (private)   │  Cloud Run, ingress: internal only
│ service-b           │  reads APP_SECRET from Secret Manager
└────────────────────┘
```

One-time GCP provisioning (service accounts, secret, IAM bindings, Cloud
Build trigger) was run locally via `gcloud`; the exact commands are inlined
below with placeholders so the setup is reproducible without committing a
real project ID or email to the repo.

## Repo layout

```
service-a/       frontend (public) — Flask + gunicorn
service-b/       backend (private) — Flask + gunicorn
cloudbuild.yaml  build + push + deploy, triggered on push to main
```

## Setup / commands

Prerequisites: `gcloud` CLI installed and authenticated (`gcloud init`), a
GCP project with billing enabled, and this repo pushed to your own GitHub
account.

1. **Set config values** (replace the placeholders):
   ```bash
   export PROJECT_ID=<your-gcp-project-id>
   export REGION=us-central1
   export AR_REPO=app-images
   export SA_FRONTEND=sa-service-a
   export SA_BACKEND=sa-service-b
   export SA_CLOUDBUILD=sa-cloudbuild-deploy
   export SECRET_NAME=APP_SECRET
   export NETWORK=app-network
   export SUBNET=app-subnet
   export GITHUB_OWNER=<your-github-username>
   export GITHUB_REPO=gcp-devops-takehome
   export GH_CONNECTION=gh-connection
   export NOTIFICATION_EMAIL=<your-email>

   gcloud config set project "$PROJECT_ID"
   PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
   ```

2. **Enable required APIs:**
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
     artifactregistry.googleapis.com secretmanager.googleapis.com \
     compute.googleapis.com iam.googleapis.com monitoring.googleapis.com
   ```

3. **Artifact Registry repo:**
   ```bash
   gcloud artifacts repositories create "$AR_REPO" \
     --repository-format=docker --location="$REGION"
   ```

4. **Dedicated runtime service accounts** (not the default compute SA):
   ```bash
   gcloud iam service-accounts create "$SA_FRONTEND" --display-name="service-a (frontend) runtime SA"
   gcloud iam service-accounts create "$SA_BACKEND" --display-name="service-b (backend) runtime SA"
   ```

5. **Secret** (random value, generated and piped straight in — never displayed or written to disk):
   ```bash
   openssl rand -base64 32 | gcloud secrets create "$SECRET_NAME" \
     --replication-policy=automatic --data-file=-

   gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
     --member="serviceAccount:${SA_BACKEND}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

6. **VPC network and subnet** for Direct VPC egress (this project has no
   default VPC network, so a minimal custom one is needed):
   ```bash
   gcloud compute networks create "$NETWORK" --subnet-mode=custom
   gcloud compute networks subnets create "$SUBNET" \
     --network="$NETWORK" --region="$REGION" --range=10.10.0.0/24
   ```

7. **Dedicated Cloud Build service account**, scoped to exactly what the
   build steps do — deploy Cloud Run revisions, push images, deploy *as* the
   two runtime SAs, and read `APP_SECRET` (deploying a revision with
   `--set-secrets` requires the deploying principal to hold `secretAccessor`
   too, separately from the runtime SA's own access):
   ```bash
   gcloud iam service-accounts create "$SA_CLOUDBUILD" \
     --display-name="Cloud Build deploy SA (dedicated, not the legacy default)"

   CB_SA_EMAIL="${SA_CLOUDBUILD}@${PROJECT_ID}.iam.gserviceaccount.com"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/run.admin" --condition=None
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/artifactregistry.writer" --condition=None
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/artifactregistry.reader" --condition=None
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/logging.logWriter" --condition=None
   gcloud iam service-accounts add-iam-policy-binding "${SA_FRONTEND}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/iam.serviceAccountUser"
   gcloud iam service-accounts add-iam-policy-binding "${SA_BACKEND}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/iam.serviceAccountUser"
   gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
     --member="serviceAccount:${CB_SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
   ```
   `artifactregistry.reader` lets the build pull the standard Google-provided
   builder images used below, which are Artifact-Registry-backed.

8. **Connect the GitHub repo to Cloud Build** (2nd-gen, Developer Connect).
   This is the one step that needs a browser — `gcloud builds connections create`
   prints an authorization URL; open it, log in to GitHub, and authorize the
   Cloud Build GitHub App before continuing:
   ```bash
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com" \
     --role="roles/secretmanager.admin" --condition=None  # lets the connection store its OAuth token

   gcloud builds connections create github "$GH_CONNECTION" --region="$REGION"
   # -> open the printed URL, authorize, then continue once it shows COMPLETE:
   gcloud builds connections describe "$GH_CONNECTION" --region="$REGION" --format='value(installationState.stage)'

   gcloud builds repositories create "$GITHUB_REPO" \
     --remote-uri="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git" \
     --connection="$GH_CONNECTION" --region="$REGION"

   gcloud builds triggers create github \
     --name="deploy-on-push" \
     --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${GH_CONNECTION}/repositories/${GITHUB_REPO}" \
     --branch-pattern="^main$" --build-config="cloudbuild.yaml" --region="$REGION" \
     --service-account="projects/${PROJECT_ID}/serviceAccounts/${CB_SA_EMAIL}"
   ```

9. **Push to `main`** — this fires the first build via the trigger:
   ```bash
   git push origin main
   ```
   Watch it in Cloud Build console or `gcloud builds list --ongoing`.

10. **Set up monitoring** (needs the frontend's live URL, so run after step 9):
   ```bash
   FRONTEND_URL=$(gcloud run services describe service-a --region="$REGION" --format='value(status.url)')
   FRONTEND_HOST=${FRONTEND_URL#https://}

   CHANNEL_ID=$(gcloud beta monitoring channels create --display-name="devops-takehome-alerts" \
     --type=email --channel-labels=email_address="$NOTIFICATION_EMAIL" --format='value(name)')

   CHECK_NAME=$(gcloud monitoring uptime create "service-a-uptime" --resource-type=uptime-url \
     --protocol=https --hostname="$FRONTEND_HOST" --path="/" --period=5 --format='value(name)')
   CHECK_ID="${CHECK_NAME##*/}"

   cat > /tmp/uptime-alert-policy.json <<EOF
   {
     "displayName": "service-a uptime failure",
     "combiner": "OR",
     "conditions": [{
       "displayName": "Uptime check failing",
       "conditionThreshold": {
         "filter": "resource.type=\"uptime_url\" AND metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.\"check_id\"=\"${CHECK_ID}\"",
         "comparison": "COMPARISON_LT", "thresholdValue": 1, "duration": "0s",
         "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_FRACTION_TRUE",
           "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.host"]}]
       }
     }],
     "notificationChannels": ["${CHANNEL_ID}"],
     "alertStrategy": {"autoClose": "1800s"}
   }
   EOF
   gcloud alpha monitoring policies create --policy-from-file=/tmp/uptime-alert-policy.json
   rm /tmp/uptime-alert-policy.json
   ```

11. **Verify:**
    ```bash
    # Should succeed, with secret_loaded: true
    curl https://<service-a-url>/

    # Should fail (403/timeout) — backend is not publicly reachable
    curl https://<service-b-url>/data
    ```

12. **Tear down when done** — reverse of steps 2–10: delete the Cloud Run
    services, alert policy, uptime check, notification channel, trigger,
    repository link, connection, secret (including the connection's
    auto-created GitHub OAuth token secret), Artifact Registry repo,
    VPC subnet + network, service accounts (including the Cloud Build one),
    and the project-level IAM bindings granted in steps 6–8.

## IAM design

| Service account | Roles | Scope |
|---|---|---|
| `sa-service-a` (frontend runtime SA) | `roles/run.invoker` | Bound only on `service-b`, not project-wide — the frontend can invoke exactly the one backend it needs. |
| `sa-service-b` (backend runtime SA) | `roles/secretmanager.secretAccessor` | Bound only on the `APP_SECRET` secret resource, not all secrets in the project. |
| `sa-cloudbuild-deploy` (dedicated Cloud Build SA) | `roles/run.admin`, `roles/artifactregistry.writer`, `roles/artifactregistry.reader`, `roles/logging.logWriter` (all project-level — this identity's whole job is deploying/pushing across the project), `roles/iam.serviceAccountUser`, `roles/secretmanager.secretAccessor` | The last two are resource-scoped: `serviceAccountUser` only on `sa-service-a`/`sa-service-b` (so Build can deploy revisions *as* them, nothing broader), `secretAccessor` only on `APP_SECRET` (needed because `gcloud run deploy --set-secrets` validates the *deploying* principal's access at deploy time, separately from the runtime SA's own access). |

Neither Cloud Run service uses the default compute service account, and
Cloud Build uses a dedicated SA rather than its own default one.
`run.invoker` on the backend is re-applied idempotently on every build
(`cloudbuild.yaml`'s `bind-invoker` step) so it's correct from the very
first deploy, not a manual one-off `gcloud` call.

## How the backend is protected

- `service-b` is deployed with `--ingress=internal` and **without**
  `--allow-unauthenticated`. A direct `curl` to its URL from a laptop fails
  both on network reachability (internal ingress) and on auth (401/403 if
  it were reachable).
- `service-a` reaches it using **Direct VPC egress** (`--vpc-egress=all-traffic`).
  Chosen over a Serverless VPC Access connector because it's the currently
  recommended approach — no connector to provision, run, or pay for, and
  lower latency.
- Before calling the backend, `service-a` fetches a Google-issued **ID
  token** scoped to the backend's URL (`google.oauth2.id_token.fetch_id_token`,
  using its own runtime SA's identity via the metadata server — no key
  file involved) and sends it as `Authorization: Bearer <token>`. Cloud Run
  validates the token and checks the caller against `run.invoker` on
  `service-b`, which only `sa-service-a` holds.

## Secret handling

`service-b` reads `APP_SECRET` from an environment variable, but that
variable is populated by Cloud Run's native Secret Manager integration
(`--set-secrets=APP_SECRET=APP_SECRET:latest`) — the value is resolved at
container start directly from Secret Manager, not baked into the image,
not a plaintext env var in the deploy config, and never committed to git.
Only `sa-service-b` can read it (`secretAccessor` scoped to that secret).
The `/data` endpoint returns a SHA-256 fingerprint of the secret, never the
raw value, so even a compromised frontend or logging pipeline can't recover
it from the response.

## CI/CD

`cloudbuild.yaml`, triggered by a Cloud Build push trigger on `main`
(created in setup step 8 above):

1. Build `service-b` and `service-a` images (tagged `$SHORT_SHA`)
2. Push both to Artifact Registry
3. Deploy `service-b` (internal ingress, secret from Secret Manager, dedicated SA)
4. Re-bind `run.invoker` on `service-b` for the frontend SA (idempotent)
5. Look up `service-b`'s URL and deploy `service-a` with it as `BACKEND_URL`, over Direct VPC egress

No manual `gcloud run deploy` is used for actual deployments — only the
one-time IAM/resource setup (steps 1–8 above) was run by hand, locally.

## Monitoring

An uptime check on `service-a`'s public URL (HTTPS, `/`, checked every 5
minutes from multiple regions) feeds an alert policy that fires when the
check's success fraction drops below 100% over a 5-minute window, notifying
an email channel. Chosen because it's the cheapest, most direct signal that
"is the one public entrypoint actually up" — for a two-service demo app,
that's the failure mode that matters most (versus, say, a 5xx-rate alert
on request volume that's near zero most of the time at min-instances=0).

## Cost

- Both services deploy with `--min-instances=0` — scale to zero, no idle cost.
- Artifact Registry storage and a handful of Cloud Build minutes are the
  only steady-state costs, both negligible at this scale.
- *(Fill in after running: approximate cost incurred, and confirmation
  teardown was run.)*

## With more time, I'd...

- Add VPC firewall rules / tightened egress on the frontend beyond routing
  through the VPC (e.g. restrict egress to only what's needed instead of
  `all-traffic`).
- Add a billing budget alert (skipped — no billing IAM granted in this
  scratch project).
- Write a smoke test step in `cloudbuild.yaml` that curls the frontend post-deploy
  and fails the build if `secret_loaded` isn't `true`, instead of relying on
  manual verification.
- Pin base image digests in the Dockerfiles instead of a mutable tag
  (`python:3.12-slim`) for reproducible builds.
- Use Terraform instead of shell scripts for the one-time infra, so the
  setup is declarative and diffable rather than imperative.
