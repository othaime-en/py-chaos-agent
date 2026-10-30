# Remote state in S3, locked with a lock file next to the state object, so two
# applies cannot run at once and the state is not on anyone's laptop.
#
# This is a partial configuration: the bucket belongs to you, so it is not
# hardcoded. Copy backend.hcl.example to backend.hcl, set your bucket, then:
#
#   terraform init -backend-config=backend.hcl
#
# The bucket must exist before `terraform init`. See docs/user-guide/terraform.md
# for the commands that create it with versioning and encryption.
terraform {
  backend "s3" {}
}
