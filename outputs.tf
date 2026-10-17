output "site_url" {
  description = "Address of the page"
  value       = local.site_url
}

output "cloudfront_url" {
  description = "CloudFront address of the page"
  value       = "https://${aws_cloudfront_distribution.web.domain_name}"
}

output "user_pool_id" {
  description = "Cognito user pool of the page"
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "Public app client id used by the browser"
  value       = aws_cognito_user_pool_client.web.id
}

output "function_name" {
  description = "Lambda function of the API"
  value       = aws_lambda_function.api.function_name
}

output "table_name" {
  description = "DynamoDB table with the address books, their history and the sync state"
  value       = aws_dynamodb_table.main.name
}

output "dav_url" {
  description = "CardDAV server for Apple Contacts, Thunderbird and other clients; empty without dav_domain_name"
  value       = local.dav ? "https://${var.dav_domain_name}" : ""
}
