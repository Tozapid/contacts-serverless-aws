resource "tls_private_key" "photos" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "photos" {
  provider    = aws.global
  name        = "${local.name}-photos"
  encoded_key = tls_private_key.photos.public_key_pem
}

resource "aws_cloudfront_key_group" "photos" {
  provider = aws.global
  name     = "${local.name}-photos"
  items    = [aws_cloudfront_public_key.photos.id]
}

resource "aws_ssm_parameter" "photos_signing_key" {
  #checkov:skip=CKV_AWS_337:The AWS managed SSM key is enough for this CloudFront signing key.
  name        = "/${local.name}/cloudfront-private-key"
  description = "Private key used to sign CloudFront cookies for contact photos."
  type        = "SecureString"
  value       = tls_private_key.photos.private_key_pem
}

resource "aws_cloudfront_function" "api_host" {
  provider = aws.global
  name     = "${local.name}-api-host"
  runtime  = "cloudfront-js-2.0"
  publish  = true
  comment  = "Remember the viewer host so photo cookies match the site address."
  code     = file("${path.module}/cloudfront/api_host.js")
}

resource "aws_cloudfront_cache_policy" "photos" {
  provider    = aws.global
  name        = "${local.name}-photos"
  comment     = "Cache photos by path. The signed cookie is checked on every request and is not part of the cache key."
  default_ttl = 86400
  max_ttl     = 31536000
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    headers_config {
      header_behavior = "none"
    }

    cookies_config {
      cookie_behavior = "none"
    }

    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "photos" {
  provider = aws.global
  name     = "${local.name}-photos"

  security_headers_config {
    content_type_options {
      override = true
    }

    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }

    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }

    content_security_policy {
      content_security_policy = local.photos_csp
      override                = true
    }
  }

  custom_headers_config {
    items {
      header   = "Cache-Control"
      override = true
      value    = "private, max-age=86400"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  provider                          = aws.global
  name                              = local.web_bucket
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_cache_policy" "web" {
  provider    = aws.global
  name        = "${local.name}-web"
  comment     = "Short cache for the page. API responses are not cached."
  default_ttl = 0
  max_ttl     = 86400
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    headers_config {
      header_behavior = "none"
    }

    cookies_config {
      cookie_behavior = "none"
    }

    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "web" {
  provider = aws.global
  name     = "${local.name}-web"

  security_headers_config {
    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }

    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }

    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
  }
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  provider = aws.global
  name     = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  provider = aws.global
  name     = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "web" {
  #checkov:skip=CKV_AWS_374: "Ensure AWS CloudFront web distribution has geo restriction enabled"
  #checkov:skip=CKV_AWS_68:WAFv2 costs about the whole monthly budget of this account.
  #checkov:skip=CKV2_AWS_47:No WAFv2 WebACL is attached.
  #checkov:skip=CKV_AWS_310:One S3 origin per bucket and one API origin are enough.
  #checkov:skip=CKV_AWS_86:The access logs are standard logging v2 (access-logs.tf); the legacy logging block needs a bucket ACL.
  #checkov:skip=CKV_AWS_305:Geo restriction is not required for this private app.
  #checkov:skip=CKV_AWS_174:Without a domain (the demo) the CloudFront certificate allows only its own default protocols.
  provider            = aws.global
  enabled             = true
  is_ipv6_enabled     = true
  aliases             = local.custom_domain ? [var.domain_name] : []
  comment             = local.name
  price_class         = "PriceClass_100"
  default_root_object = "index.html"
  http_version        = "http2and3"
  wait_for_deployment = true

  origin {
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_id                = "web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
    connection_attempts      = 3
    connection_timeout       = 10
  }

  origin {
    domain_name              = aws_s3_bucket.photos.bucket_regional_domain_name
    origin_id                = "photos"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
    connection_attempts      = 3
    connection_timeout       = 10
  }

  origin {
    domain_name = "${aws_apigatewayv2_api.http.id}.execute-api.${var.region}.amazonaws.com"
    origin_id   = "api"

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 30
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = "web"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    cache_policy_id            = aws_cloudfront_cache_policy.web.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web.id
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    viewer_protocol_policy   = "redirect-to-https"
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.api_host.arn
    }
  }

  # Objects keep their bucket key, so /photos/<user>/<id>.jpg needs no rewrite.
  ordered_cache_behavior {
    path_pattern           = "/photos/*"
    target_origin_id       = "photos"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true
    cache_policy_id        = aws_cloudfront_cache_policy.photos.id
    trusted_key_groups     = [aws_cloudfront_key_group.photos.id]

    response_headers_policy_id = aws_cloudfront_response_headers_policy.photos.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Without a domain (the demo) the page answers at *.cloudfront.net with
  # CloudFront's own certificate.
  viewer_certificate {
    acm_certificate_arn            = local.custom_domain ? aws_acm_certificate_validation.web[0].certificate_arn : null
    cloudfront_default_certificate = !local.custom_domain
    ssl_support_method             = local.custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }
}
