# Pinned, not floating. A provider minor bump has changed resource defaults before, and an
# IaC run that produces a different plan on a different day is not reproducible.
terraform {
  required_version = "~> 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Deliberately NOT configured with a real backend. State belongs in S3 with DynamoDB
  # locking for any shared use, but committing a bucket name here would either name someone
  # else's bucket or invent one that does not exist. Documented in README.md instead, so the
  # omission is a decision rather than an oversight.
  # backend "s3" { ... }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "fleet-idle-cost"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Cost attribution is not optional on a personal account. Every resource carries this
      # so a budget alarm can say WHAT is costing money, not just that something is.
      CostCenter = "hackathon"
    }
  }
}
