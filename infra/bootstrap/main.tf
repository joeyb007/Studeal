# Bootstrap: the two resources that hold Terraform's memory for everything
# else — created FIRST, with local state (the chicken-and-egg resolution:
# the state bucket can't store the state of its own creation).
#
#   terraform init && terraform apply        (run once, from infra/bootstrap)
#
# Everything else lives in infra/ and uses the S3 backend these create.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}

locals {
  # S3 bucket names are GLOBALLY unique across all AWS accounts — suffixing
  # with the account id avoids collisions with anyone else's "studeal".
  state_bucket = "studeal-tf-state-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "tf_state" {
  bucket = local.state_bucket
}

# Versioning: every state write keeps the previous version — the undo button
# for a corrupted or mistakenly-changed state file.
resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The lock: `terraform apply` writes a row here before touching state and
# deletes it after. A second concurrent apply sees the row and refuses to
# run — two writers can't corrupt the state file.
resource "aws_dynamodb_table" "tf_lock" {
  name         = "studeal-tf-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

output "state_bucket" {
  value = aws_s3_bucket.tf_state.bucket
}

output "lock_table" {
  value = aws_dynamodb_table.tf_lock.name
}
