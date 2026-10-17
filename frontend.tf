locals {
  web_assets = {
    "index.html"  = "text/html; charset=utf-8"
    "app.js"      = "text/javascript; charset=utf-8"
    "fields.js"   = "text/javascript; charset=utf-8"
    "i18n.js"     = "text/javascript; charset=utf-8"
    "styles.css"  = "text/css; charset=utf-8"
    "favicon.svg" = "image/svg+xml"
  }
}

resource "aws_s3_object" "web" {
  for_each = local.web_assets

  bucket        = aws_s3_bucket.web.id
  key           = each.key
  source        = "${path.module}/web/${each.key}"
  etag          = filemd5("${path.module}/web/${each.key}")
  content_type  = each.value
  cache_control = each.key == "favicon.svg" ? "public, max-age=86400" : "no-cache"
}

resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.web.id
  key           = "config.js"
  content_type  = "text/javascript; charset=utf-8"
  cache_control = "no-cache"
  content = templatefile("${path.module}/web/config.js.tftpl", {
    region       = var.region
    user_pool_id = aws_cognito_user_pool.main.id
    client_id    = aws_cognito_user_pool_client.web.id
    domain       = local.site_host
    # The demo publishes its sign-in on the sign-in screen; nothing else is.
    demo = jsonencode(var.demo ? {
      email    = var.admin_email
      password = var.admin_password
      reset    = local.reset
    } : null)
  })
}

# The privacy policy and the terms of service, with the site and the address
# to write to.
resource "aws_s3_object" "legal" {
  for_each = toset(["privacy.html", "terms.html"])

  bucket        = aws_s3_bucket.web.id
  key           = each.key
  content_type  = "text/html; charset=utf-8"
  cache_control = "no-cache"
  content = templatefile("${path.module}/web/${each.key}.tftpl", {
    domain = local.site_host
    email  = var.admin_email
  })
}
