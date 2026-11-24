# The CI writes site_url into the summary of a deploy and the comment of a
# preview.
output "site_url" {
  description = "Address of the page."
  value       = module.contacts.site_url
}

output "cloudfront_url" {
  description = "CloudFront address of the page."
  value       = module.contacts.cloudfront_url
}

output "admin_username" {
  description = "Email of the demo user."
  value       = module.contacts.admin_username
}
