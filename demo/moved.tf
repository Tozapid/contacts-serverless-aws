# The demo kept its resources at the top of the state until the repository
# became a module (v0.4.0): its state keeps every resource, now under
# module.contacts. The CloudFront key of the photos is made only without
# photos_key_from, so it has an index there.

moved {
  from = aws_cloudwatch_log_delivery_source.access
  to   = module.contacts.aws_cloudwatch_log_delivery_source.access
}

moved {
  from = aws_cloudwatch_log_delivery_destination.access
  to   = module.contacts.aws_cloudwatch_log_delivery_destination.access
}

moved {
  from = aws_cloudwatch_log_delivery.access
  to   = module.contacts.aws_cloudwatch_log_delivery.access
}

moved {
  from = aws_apigatewayv2_api.http
  to   = module.contacts.aws_apigatewayv2_api.http
}

moved {
  from = aws_apigatewayv2_authorizer.cognito
  to   = module.contacts.aws_apigatewayv2_authorizer.cognito
}

moved {
  from = aws_apigatewayv2_integration.lambda
  to   = module.contacts.aws_apigatewayv2_integration.lambda
}

moved {
  from = aws_apigatewayv2_route.api
  to   = module.contacts.aws_apigatewayv2_route.api
}

moved {
  from = aws_cloudwatch_log_group.api
  to   = module.contacts.aws_cloudwatch_log_group.api
}

moved {
  from = aws_apigatewayv2_stage.default
  to   = module.contacts.aws_apigatewayv2_stage.default
}

moved {
  from = aws_lambda_permission.api
  to   = module.contacts.aws_lambda_permission.api
}

moved {
  from = aws_cloudwatch_log_group.dav
  to   = module.contacts.aws_cloudwatch_log_group.dav
}

moved {
  from = aws_cloudwatch_log_group.dav_api
  to   = module.contacts.aws_cloudwatch_log_group.dav_api
}

moved {
  from = aws_lambda_function.dav
  to   = module.contacts.aws_lambda_function.dav
}

moved {
  from = aws_apigatewayv2_api.dav
  to   = module.contacts.aws_apigatewayv2_api.dav
}

moved {
  from = aws_apigatewayv2_integration.dav
  to   = module.contacts.aws_apigatewayv2_integration.dav
}

moved {
  from = aws_apigatewayv2_route.dav
  to   = module.contacts.aws_apigatewayv2_route.dav
}

moved {
  from = aws_apigatewayv2_stage.dav
  to   = module.contacts.aws_apigatewayv2_stage.dav
}

moved {
  from = aws_lambda_permission.dav
  to   = module.contacts.aws_lambda_permission.dav
}

moved {
  from = aws_acm_certificate.dav
  to   = module.contacts.aws_acm_certificate.dav
}

moved {
  from = aws_route53_record.dav_certificate
  to   = module.contacts.aws_route53_record.dav_certificate
}

moved {
  from = aws_acm_certificate_validation.dav
  to   = module.contacts.aws_acm_certificate_validation.dav
}

moved {
  from = aws_apigatewayv2_domain_name.dav
  to   = module.contacts.aws_apigatewayv2_domain_name.dav
}

moved {
  from = aws_apigatewayv2_api_mapping.dav
  to   = module.contacts.aws_apigatewayv2_api_mapping.dav
}

moved {
  from = aws_route53_record.dav_ipv4
  to   = module.contacts.aws_route53_record.dav_ipv4
}

moved {
  from = tls_private_key.photos
  to   = module.contacts.tls_private_key.photos[0]
}

moved {
  from = aws_cloudfront_public_key.photos
  to   = module.contacts.aws_cloudfront_public_key.photos[0]
}

moved {
  from = aws_cloudfront_key_group.photos
  to   = module.contacts.aws_cloudfront_key_group.photos[0]
}

moved {
  from = aws_ssm_parameter.photos_signing_key
  to   = module.contacts.aws_ssm_parameter.photos_signing_key[0]
}

moved {
  from = aws_cloudfront_function.api_host
  to   = module.contacts.aws_cloudfront_function.api_host
}

moved {
  from = aws_cloudfront_cache_policy.photos
  to   = module.contacts.aws_cloudfront_cache_policy.photos
}

moved {
  from = aws_cloudfront_response_headers_policy.photos
  to   = module.contacts.aws_cloudfront_response_headers_policy.photos
}

moved {
  from = aws_cloudfront_origin_access_control.web
  to   = module.contacts.aws_cloudfront_origin_access_control.web
}

moved {
  from = aws_cloudfront_cache_policy.web
  to   = module.contacts.aws_cloudfront_cache_policy.web
}

moved {
  from = aws_cloudfront_response_headers_policy.web
  to   = module.contacts.aws_cloudfront_response_headers_policy.web
}

moved {
  from = aws_cloudfront_distribution.web
  to   = module.contacts.aws_cloudfront_distribution.web
}

moved {
  from = aws_cognito_user_pool.main
  to   = module.contacts.aws_cognito_user_pool.main
}

moved {
  from = aws_cognito_user_pool_client.web
  to   = module.contacts.aws_cognito_user_pool_client.web
}

moved {
  from = aws_cognito_user.admin
  to   = module.contacts.aws_cognito_user.admin
}

moved {
  from = aws_cognito_user_group.admins
  to   = module.contacts.aws_cognito_user_group.admins
}

moved {
  from = aws_cognito_user_in_group.admin
  to   = module.contacts.aws_cognito_user_in_group.admin
}

moved {
  from = aws_acm_certificate.web
  to   = module.contacts.aws_acm_certificate.web
}

moved {
  from = aws_route53_record.certificate
  to   = module.contacts.aws_route53_record.certificate
}

moved {
  from = aws_acm_certificate_validation.web
  to   = module.contacts.aws_acm_certificate_validation.web
}

moved {
  from = aws_route53_record.web_ipv4
  to   = module.contacts.aws_route53_record.web_ipv4
}

moved {
  from = aws_route53_record.web_ipv6
  to   = module.contacts.aws_route53_record.web_ipv6
}

moved {
  from = aws_dynamodb_table.main
  to   = module.contacts.aws_dynamodb_table.main
}

moved {
  from = aws_s3_object.web
  to   = module.contacts.aws_s3_object.web
}

moved {
  from = aws_s3_object.config
  to   = module.contacts.aws_s3_object.config
}

moved {
  from = aws_s3_object.legal
  to   = module.contacts.aws_s3_object.legal
}

moved {
  from = aws_cloudwatch_log_group.google
  to   = module.contacts.aws_cloudwatch_log_group.google
}

moved {
  from = aws_lambda_function.google
  to   = module.contacts.aws_lambda_function.google
}

moved {
  from = aws_lambda_function_event_invoke_config.google
  to   = module.contacts.aws_lambda_function_event_invoke_config.google
}

moved {
  from = aws_cloudwatch_event_rule.google
  to   = module.contacts.aws_cloudwatch_event_rule.google
}

moved {
  from = aws_cloudwatch_event_target.google
  to   = module.contacts.aws_cloudwatch_event_target.google
}

moved {
  from = aws_lambda_permission.google
  to   = module.contacts.aws_lambda_permission.google
}

moved {
  from = aws_apigatewayv2_route.google_callback
  to   = module.contacts.aws_apigatewayv2_route.google_callback
}

moved {
  from = aws_iam_role.lambda
  to   = module.contacts.aws_iam_role.lambda
}

moved {
  from = aws_iam_role_policy.lambda
  to   = module.contacts.aws_iam_role_policy.lambda
}

moved {
  from = aws_cloudwatch_log_group.icloud
  to   = module.contacts.aws_cloudwatch_log_group.icloud
}

moved {
  from = aws_lambda_function.icloud
  to   = module.contacts.aws_lambda_function.icloud
}

moved {
  from = aws_lambda_function_event_invoke_config.icloud
  to   = module.contacts.aws_lambda_function_event_invoke_config.icloud
}

moved {
  from = aws_cloudwatch_event_rule.icloud
  to   = module.contacts.aws_cloudwatch_event_rule.icloud
}

moved {
  from = aws_cloudwatch_event_target.icloud
  to   = module.contacts.aws_cloudwatch_event_target.icloud
}

moved {
  from = aws_lambda_permission.icloud
  to   = module.contacts.aws_lambda_permission.icloud
}

moved {
  from = aws_cloudwatch_log_group.lambda
  to   = module.contacts.aws_cloudwatch_log_group.lambda
}

moved {
  from = aws_lambda_function.api
  to   = module.contacts.aws_lambda_function.api
}

moved {
  from = aws_ssm_parameter.admin_password
  to   = module.contacts.aws_ssm_parameter.admin_password
}

moved {
  from = aws_cloudwatch_log_group.reset
  to   = module.contacts.aws_cloudwatch_log_group.reset
}

moved {
  from = aws_iam_role.reset
  to   = module.contacts.aws_iam_role.reset
}

moved {
  from = aws_iam_role_policy.reset
  to   = module.contacts.aws_iam_role_policy.reset
}

moved {
  from = aws_lambda_function.reset
  to   = module.contacts.aws_lambda_function.reset
}

moved {
  from = aws_cloudwatch_event_rule.reset
  to   = module.contacts.aws_cloudwatch_event_rule.reset
}

moved {
  from = aws_cloudwatch_event_target.reset
  to   = module.contacts.aws_cloudwatch_event_target.reset
}

moved {
  from = aws_lambda_permission.reset
  to   = module.contacts.aws_lambda_permission.reset
}

moved {
  from = aws_lambda_invocation.first_reset
  to   = module.contacts.aws_lambda_invocation.first_reset
}

moved {
  from = aws_s3_bucket.web
  to   = module.contacts.aws_s3_bucket.web
}

moved {
  from = aws_s3_bucket_public_access_block.web
  to   = module.contacts.aws_s3_bucket_public_access_block.web
}

moved {
  from = aws_s3_bucket_ownership_controls.web
  to   = module.contacts.aws_s3_bucket_ownership_controls.web
}

moved {
  from = aws_s3_bucket_versioning.web
  to   = module.contacts.aws_s3_bucket_versioning.web
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.web
  to   = module.contacts.aws_s3_bucket_server_side_encryption_configuration.web
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.web
  to   = module.contacts.aws_s3_bucket_lifecycle_configuration.web
}

moved {
  from = aws_s3_bucket_policy.web
  to   = module.contacts.aws_s3_bucket_policy.web
}

moved {
  from = aws_s3_bucket.photos
  to   = module.contacts.aws_s3_bucket.photos
}

moved {
  from = aws_s3_bucket_public_access_block.photos
  to   = module.contacts.aws_s3_bucket_public_access_block.photos
}

moved {
  from = aws_s3_bucket_ownership_controls.photos
  to   = module.contacts.aws_s3_bucket_ownership_controls.photos
}

moved {
  from = aws_s3_bucket_versioning.photos
  to   = module.contacts.aws_s3_bucket_versioning.photos
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.photos
  to   = module.contacts.aws_s3_bucket_server_side_encryption_configuration.photos
}

moved {
  from = aws_s3_bucket_cors_configuration.photos
  to   = module.contacts.aws_s3_bucket_cors_configuration.photos
}

moved {
  from = aws_s3_bucket_policy.photos
  to   = module.contacts.aws_s3_bucket_policy.photos
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.photos
  to   = module.contacts.aws_s3_bucket_lifecycle_configuration.photos
}

moved {
  from = aws_s3_bucket_logging.photos
  to   = module.contacts.aws_s3_bucket_logging.photos
}

moved {
  from = aws_sesv2_email_identity.alerts
  to   = module.contacts.aws_sesv2_email_identity.alerts
}

moved {
  from = aws_route53_record.alerts_dkim
  to   = module.contacts.aws_route53_record.alerts_dkim
}

moved {
  from = aws_sesv2_email_identity_mail_from_attributes.alerts
  to   = module.contacts.aws_sesv2_email_identity_mail_from_attributes.alerts
}

moved {
  from = aws_route53_record.alerts_mail_from_mx
  to   = module.contacts.aws_route53_record.alerts_mail_from_mx
}

moved {
  from = aws_route53_record.alerts_mail_from_spf
  to   = module.contacts.aws_route53_record.alerts_mail_from_spf
}

moved {
  from = aws_route53_record.alerts_dmarc
  to   = module.contacts.aws_route53_record.alerts_dmarc
}
