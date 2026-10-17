locals {
  name          = var.name
  account_id    = data.aws_caller_identity.current.account_id
  web_bucket    = "${local.name}-web-${local.account_id}"
  photos_bucket = "${local.name}-photos-${local.account_id}"

  # The page on its own domain, or on the CloudFront address without one.
  custom_domain = var.domain_name != ""
  site_host     = local.custom_domain ? var.domain_name : aws_cloudfront_distribution.web.domain_name
  site_url      = "https://${local.site_host}"

  # What runs: the CardDAV server with a name of its own, the sync with
  # Google with a client id, the sync with iCloud when it is on. The demo has
  # none of them; its page shows the sync without running it.
  dav    = var.dav_domain_name != ""
  google = var.google_client_id != ""
  icloud = var.icloud_enabled
  reset  = var.reset_schedule != ""

  # The version of this deployment: the newest entry of CHANGELOG.md.
  version = regex("(?m)^## (v[0-9]+\\.[0-9]+\\.[0-9]+)(?:\\s|$)", file("${path.module}/CHANGELOG.md"))[0]

  # Google sync (google.tf). The client secret is put into SSM by hand; each
  # user's refresh token is a parameter of its own under the prefix.
  google_secret_parameter = "/${local.name}/google-client-secret"
  google_token_prefix     = "/${local.name}/google"
  google_function         = "${local.name}-google"
  # iCloud sync (icloud.tf): each user's Apple ID and app-specific password
  # are a parameter of their own under the prefix.
  icloud_password_prefix = "/${local.name}/icloud"
  icloud_function        = "${local.name}-icloud"

  csp = join("; ", [
    "default-src 'self'",
    # Only the two libraries of the page, not all of jsDelivr.
    "script-src 'self' https://cdn.jsdelivr.net/npm/amazon-cognito-identity-js@6.3.15/ https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/",
    "style-src 'self'",
    "img-src 'self' blob: data:",
    "connect-src 'self' https://cognito-idp.${var.region}.amazonaws.com https://*.s3.${var.region}.amazonaws.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ])
  # Photos are served from the site origin. The sandbox keeps a file that is
  # not an image from running script with the site's cookies.
  photos_csp = "default-src 'none'; img-src 'self' data:; sandbox"
}
