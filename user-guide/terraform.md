# Terraform (AWS EKS)

`terraform/` builds a demo EKS cluster, a VPC and two ECR repositories.

## Remote state

State lives in S3 with native locking (Terraform 1.10 or newer), not on your laptop.
The bucket is yours, so `terraform/backend.tf` only declares the backend and you supply the details.

Create the bucket once, with versioning, encryption and no public access:

```bash
BUCKET=my-org-terraform-state
REGION=us-east-1

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
# Outside us-east-1, add: --create-bucket-configuration LocationConstraint="$REGION"

aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Then point Terraform at it:

```bash
cd terraform
cp backend.hcl.example backend.hcl      # set your bucket name; backend.hcl is git-ignored
terraform init -backend-config=backend.hcl
```

Already have local state? Add `-migrate-state` to the `init` command and answer `yes`.

Commit `.terraform.lock.hcl` after the first `init`. It pins the provider versions and checksums.

## The Kubernetes API endpoint

By default the cluster API is reachable from the internet, protected by IAM. That keeps the
demo easy to start, and Terraform prints a warning on every plan about it. Tighten it:

```bash
# Only your own address
terraform apply -var 'cluster_endpoint_public_access_cidrs=["203.0.113.7/32"]'

# Private cluster: no public endpoint at all
terraform apply -var cluster_endpoint_public_access=false
```

With a private endpoint, `kubectl` only works from inside the VPC (a VPN, a bastion host or
SSM). The nodes always use the private endpoint.

| Variable | Default | Purpose |
| --- | --- | --- |
| `cluster_endpoint_private_access` | `true` | API reachable inside the VPC. Keep it on. |
| `cluster_endpoint_public_access` | `true` | API reachable from the internet. |
| `cluster_endpoint_public_access_cidrs` | `["0.0.0.0/0"]` | Who may reach the public endpoint. |

## Container registry

Both ECR repositories scan images on push, encrypt them, and use **immutable tags**: a tag
that has been pushed can never be overwritten. Push a new version for every build, matching
the tag in `k8s/chaos-demo.yaml`:

```bash
REPO=$(terraform output -json ecr_repo_urls | jq -r .chaos_agent)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
docker tag py-chaos-agent:1.0.0 "$REPO:1.0.0"
docker push "$REPO:1.0.0"
```

Pushing `:latest` twice now fails. Untagged images are removed after 7 days, and only the
newest 30 images per repository are kept.
