# The CardDAV server (lambda/carddav.py): its own HTTP API on dav_domain_name,
# straight to Lambda. CloudFront answers PROPFIND and REPORT with an error,
# while the "$default" route of an HTTP API passes every method. Clients sign
# in with HTTP Basic and an app password, so the API has no authorizer; the
# function checks the password.

resource "aws_cloudwatch_log_group" "dav" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  count = local.dav ? 1 : 0

  name              = "/aws/lambda/${local.name}-dav"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "dav_api" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  count = local.dav ? 1 : 0

  name              = "/aws/apigateway/${local.name}-dav"
  retention_in_days = 30
}

resource "aws_lambda_function" "dav" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:This function is synchronous. Failures are returned to the client, which syncs again.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints for DynamoDB, S3 and Cognito.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  count = local.dav ? 1 : 0

  function_name = "${local.name}-dav"
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "carddav.handler"
  memory_size   = 512
  timeout       = 29

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME      = aws_dynamodb_table.main.name
      PHOTOS_BUCKET   = aws_s3_bucket.photos.id
      PHOTO_MAX_BYTES = tostring(var.photo_max_bytes)
      USER_POOL_ID    = aws_cognito_user_pool.main.id
      UID_DOMAIN      = local.site_host
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.dav[0].name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

resource "aws_apigatewayv2_api" "dav" {
  count = local.dav ? 1 : 0

  name        = "${local.name}-dav"
  description = "CardDAV server of the address book."
  # Clients reach it by its own name only.
  protocol_type                = "HTTP"
  disable_execute_api_endpoint = true
}

resource "aws_apigatewayv2_integration" "dav" {
  count = local.dav ? 1 : 0

  api_id                 = aws_apigatewayv2_api.dav[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.dav[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "dav" {
  #checkov:skip=CKV_AWS_309:CardDAV clients sign in with HTTP Basic and an app password, which the function checks.
  count = local.dav ? 1 : 0

  api_id    = aws_apigatewayv2_api.dav[0].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.dav[0].id}"
}

resource "aws_apigatewayv2_stage" "dav" {
  count = local.dav ? 1 : 0

  api_id      = aws_apigatewayv2_api.dav[0].id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.dav_api[0].arn
    format = jsonencode({
      requestId = "$context.requestId"
      ip        = "$context.identity.sourceIp"
      method    = "$context.httpMethod"
      path      = "$context.path"
      status    = "$context.status"
    })
  }

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  depends_on = [aws_apigatewayv2_route.dav]
}

resource "aws_lambda_permission" "dav" {
  count = local.dav ? 1 : 0

  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dav[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.dav[0].execution_arn}/*/*"
}

# A regional API Gateway domain takes a certificate of its own region.
resource "aws_acm_certificate" "dav" {
  count = local.dav ? 1 : 0

  domain_name       = var.dav_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "dav_certificate" {
  for_each = {
    for dvo in local.dav ? aws_acm_certificate.dav[0].domain_validation_options : [] : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.web[0].zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
}

resource "aws_acm_certificate_validation" "dav" {
  count = local.dav ? 1 : 0

  certificate_arn         = aws_acm_certificate.dav[0].arn
  validation_record_fqdns = [for record in aws_route53_record.dav_certificate : record.fqdn]

  timeouts {
    create = "20m"
  }
}

resource "aws_apigatewayv2_domain_name" "dav" {
  count = local.dav ? 1 : 0

  domain_name = var.dav_domain_name

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.dav[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "dav" {
  count = local.dav ? 1 : 0

  api_id      = aws_apigatewayv2_api.dav[0].id
  domain_name = aws_apigatewayv2_domain_name.dav[0].id
  stage       = aws_apigatewayv2_stage.dav[0].id
}

resource "aws_route53_record" "dav_ipv4" {
  count = local.dav ? 1 : 0

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.web[0].zone_id
  name            = var.dav_domain_name
  type            = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.dav[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.dav[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}
