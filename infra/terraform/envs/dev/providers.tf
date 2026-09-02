terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Every taggable resource gets these; modules additionally set Name/Role
  # tags and pass local.tags where default_tags do not propagate (ASG-launched
  # instances and volumes).
  default_tags {
    tags = local.tags
  }
}
