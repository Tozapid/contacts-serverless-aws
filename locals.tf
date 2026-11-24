locals {
  name          = var.name
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.region
  web_bucket    = "${local.name}-web-${local.account_id}"
  photos_bucket = "${local.name}-photos-${local.account_id}"
  # A preview signs photo cookies with the key of the site it previews.
  photos_key_from = var.photos_key_from != null ? var.photos_key_from : try(regex("^(.+)-pr[0-9]+$", var.name)[0], "")
  own_photos_key  = local.photos_key_from == ""
  # Exactly one of the lists has an element. A conditional would not do:
  # both of its sides are evaluated, and the one left out is empty.
  photos_key = one(concat(
    [for index, group in aws_cloudfront_key_group.photos : {
      key_group_id          = group.id
      public_key_id         = aws_cloudfront_public_key.photos[index].id
      private_key_parameter = aws_ssm_parameter.photos_signing_key[index].name
    }],
    # Ids and a parameter name; the data source marks every value sensitive.
    [for parameter in data.aws_ssm_parameter.shared_photos_key : jsondecode(nonsensitive(parameter.value))],
  ))

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
  # Mails to a user whose sync with Google or iCloud fails (ses.tf), from
  # sender_address on domain_name. The demo runs no sync and sends nothing.
  alerts = !var.demo && var.sender_address != ""
  # The SES identity of domain_name, unless the caller manages it.
  ses_identity = local.alerts && var.ses_identity

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
    "connect-src 'self' https://cognito-idp.${local.region}.amazonaws.com https://*.s3.${local.region}.amazonaws.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ])
  # Photos are served from the site origin. The sandbox keeps a file that is
  # not an image from running script with the site's cookies.
  photos_csp = "default-src 'none'; img-src 'self' data:; sandbox"
}
