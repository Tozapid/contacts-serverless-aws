# Production: your own domains, the sync with devices, Google and iCloud, no
# public sign-in and no reset. Pin the module to a release tag; a newer
# release is a change of ref.
terraform {
  required_version = ">= 1.10.0"

  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

# CloudFront and its certificate live in us-east-1.
provider "aws" {
  alias  = "global"
  region = "us-east-1"
}

module "contacts" {
  source = "git::https://github.com/Tozapid/contacts-serverless-aws.git?ref=v0.4.0"

  providers = {
    aws        = aws
    aws.global = aws.global
  }

  name              = "contacts"
  domain_name       = "contacts.example.com"
  dav_domain_name   = "dav.contacts.example.com"
  route53_zone_name = "example.com"
  admin_email       = "you@example.com"

  # A web application client of a Google Cloud project with the People API;
  # its secret goes into SSM by hand (see README). "" leaves Google out.
  google_client_id = ""
  icloud_enabled   = true

  # Mails a user whose sync with Google or iCloud fails; the module makes
  # domain_name an SES identity. "" sends no mails.
  sender_address = "alert@contacts.example.com"
}

output "site_url" {
  description = "Address of the page."
  value       = module.contacts.site_url
}
