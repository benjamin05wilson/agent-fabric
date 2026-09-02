output "instance_id" {
  value = aws_instance.this.id
}

output "private_ip" {
  description = "Workers dial this address on TCP 50051."
  value       = aws_instance.this.private_ip
}

output "public_ip" {
  description = "Empty when associate_public_ip is false."
  value       = aws_instance.this.public_ip
}

output "grpc_endpoint" {
  value = "${aws_instance.this.private_ip}:50051"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.control.repository_url
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.control.arn
}

output "app_secret_arn" {
  description = "Secret holding api_key and the S3 access key pair."
  value       = aws_secretsmanager_secret.app.arn
}

output "iam_role_arn" {
  value = aws_iam_role.this.arn
}

output "s3_endpoint" {
  value = local.s3_endpoint
}
