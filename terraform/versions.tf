terraform {
  # 1.10 added native S3 state locking (use_lockfile), which backend.tf relies on.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
