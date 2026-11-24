# The public demo: the module of this repository at its CloudFront address,
# with the sign-in published on the page, the sync with Google, iCloud and
# devices only shown, no mails and the hourly reset. The CI of the repository
# deploys it from this directory.
provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}

provider "aws" {
  alias  = "global"
  region = "us-east-1"

  default_tags {
    tags = var.tags
  }
}

module "contacts" {
  source = "../"

  providers = {
    aws        = aws
    aws.global = aws.global
  }

  name               = var.name
  demo               = true
  admin_email        = "demo@example.com"
  admin_password     = "Demo-Password-1"
  reset_schedule     = "rate(1 hour)"
  access_logs_bucket = var.access_logs_bucket
  photos_key_from    = var.photos_key_from
}
