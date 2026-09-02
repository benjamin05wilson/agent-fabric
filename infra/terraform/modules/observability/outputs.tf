output "control_plane_log_group_name" {
  value = aws_cloudwatch_log_group.control_plane.name
}

output "control_plane_log_group_arn" {
  value = aws_cloudwatch_log_group.control_plane.arn
}

output "workers_log_group_name" {
  value = aws_cloudwatch_log_group.workers.name
}

output "workers_log_group_arn" {
  value = aws_cloudwatch_log_group.workers.arn
}
