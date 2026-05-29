# Deploy

Auto-deploy on push to `main`:

```
git push origin main
  → GitHub Actions builds Docker image
  → pushes to Amazon ECR (Mumbai)
  → creates/updates AWS App Runner service
  → live at https://<id>.ap-south-1.awsapprunner.com
```

## One-time setup

You only do this once. After that, every `git push origin main` redeploys.

### 1. Add GitHub Actions secrets

Repo settings → Settings → Secrets and variables → Actions → New repository secret.

| Secret name | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | the AKIA… key from the project credentials sheet |
| `AWS_SECRET_ACCESS_KEY` | the secret half of that key (from the same sheet) |
| `OPENAI_API_KEY` | your OpenAI key (`sk-...`) |
| `ANTHROPIC_API_KEY` | *(optional)* your Anthropic key (`sk-ant-...`). Skip if you don't have one — fallback will stay on OpenAI. |
| `API_SECRET_KEY` | any random string — must match `SECRET` in `frontend/index.html` |

`API_SECRET_KEY` is the `X-Secret` header the frontend sends. The frontend currently
uses `dev-secret-change-in-prod`. Either set the secret to that exact value, or
update the constant in `frontend/index.html` to match what you set.

### 2. IAM permissions on the access key

The access key needs these AWS managed policies (attach in IAM console):

- `AmazonEC2ContainerRegistryFullAccess` — push images to ECR
- `AWSAppRunnerFullAccess` — create/update App Runner services
- `IAMFullAccess` — first run creates the `AppRunnerECRAccessRole` IAM role

For prod, scope these down. For now this is the fastest path.

### 3. Push

```bash
git push origin main
```

Watch the run in the repo's Actions tab. First run takes ~6–8 min because:
- Docker image with Playwright base is ~1.5 GB
- App Runner provisioning takes ~3 min

The Summary at the end of the workflow shows the live URL.

## What the workflow does

1. Logs in to ECR with the AWS access key.
2. Creates `sarathi-agent` ECR repo if missing.
3. Builds the Docker image, tags as `:latest` and `:<git-sha>`.
4. Creates the `AppRunnerECRAccessRole` IAM role if missing.
5. If the App Runner service `sarathi-agent` does not exist → creates it
   (1 vCPU, 2 GB RAM, port 8000, health check `/health`).
   If it exists → triggers a new deployment of `:latest`.
6. Prints the live URL.

All AWS resources are tagged `Owner=sipanijai@gmail.com`.

## MVP limits (move off these later)

- **SQLite + uploads are on the container disk.** Every redeploy wipes job
  history and uploaded photos. Move `data/*.db` to RDS Postgres and `uploads/`
  to S3 once the UI is stable.
- **Single container.** All Playwright browser state lives in one process.
  No horizontal scale. Fine for a few concurrent customers, not for prod load.
- **App Runner pulls `:latest`.** If you want immutable deploys per commit,
  switch the workflow to deploy the `:<sha>` tag instead.

## Rolling back

App Runner → service → Deployments → pick a previous deployment → Redeploy.
Or push a commit that reverts the change — auto-deploy takes care of the rest.

## Costs (rough)

- App Runner: ~$0.064/hr provisioned + ~$0.007/hr active. Roughly $5–25/month
  for a small always-on workload.
- ECR: first 500 MB free, then $0.10/GB-month. Image is ~1.5 GB → ~$0.15/month.
- Outbound bandwidth: $0.09/GB after 100 GB free tier.

Region: `ap-south-1` (Mumbai).
