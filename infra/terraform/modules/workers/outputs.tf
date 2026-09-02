output "autoscaling_group_name" {
  value = aws_autoscaling_group.worker.name
}

output "launch_template_id" {
  value = aws_launch_template.worker.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.worker.arn
}

output "iam_role_arn" {
  value = aws_iam_role.worker.arn
}
