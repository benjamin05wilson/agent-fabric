output "instance_id" {
  value = aws_db_instance.this.id
}

output "address" {
  description = "DNS hostname of the instance (no port)."
  value       = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "username" {
  value = aws_db_instance.this.username
}

output "secret_arn" {
  description = "Secrets Manager secret holding host/port/dbname/username/password as JSON."
  value       = aws_secretsmanager_secret.database.arn
}
