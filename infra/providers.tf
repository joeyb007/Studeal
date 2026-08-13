# Main infrastructure config. State lives in the bucket created by
# infra/bootstrap; every resource from here on is remembered durably.
# Run all commands with AWS_PROFILE=studeal-deploy.

terraform {
  required_version = ">= 1.7"

  backend "s3" {
    bucket         = "studeal-tf-state-458781646926"
    key            = "studeal/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "studeal-tf-lock"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = "studeal"
      ManagedBy = "terraform"
    }
  }
}
