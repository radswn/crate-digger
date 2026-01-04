terraform {
  backend "s3" {
    bucket       = "radswn-tf-state-bucket"
    key          = "crate-digger/infra/terraform.tfstate"
    region       = "eu-west-1"
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  required_version = ">= 1.5.0"
}
