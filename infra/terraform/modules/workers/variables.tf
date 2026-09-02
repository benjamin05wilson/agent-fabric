variable "name_prefix" {
  description = "Prefix applied to resource names."
  type        = string
}

variable "aws_region" {
  description = "Region, used for ECR login and the awslogs driver."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs the ASG launches into."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to worker instances."
  type        = list(string)
}

variable "instance_type" {
  description = "x86_64 instance type (gVisor release binaries are x86_64/arm64; AL2023 x86_64 AMI is used)."
  type        = string
  default     = "t3.small"
}

variable "ami_id" {
  description = "Override AMI. Empty selects the latest AL2023 x86_64 AMI via SSM."
  type        = string
  default     = ""
}

variable "root_volume_gb" {
  description = "Root volume size in GiB (holds Docker images and job workspaces)."
  type        = number
  default     = 30
}

variable "min_size" {
  description = "Minimum number of workers."
  type        = number
  default     = 2
}

variable "desired_capacity" {
  description = "Desired number of workers."
  type        = number
  default     = 2
}

variable "max_size" {
  description = "Maximum number of workers."
  type        = number
  default     = 3
}

variable "image_tag" {
  description = "Tag of the worker image in ECR to run."
  type        = string
  default     = "latest"
}

variable "ecr_force_delete" {
  description = "Delete the ECR repository even if it contains images."
  type        = bool
  default     = true
}

variable "control_plane_grpc" {
  description = "host:port of the control-plane gRPC gateway (private IP)."
  type        = string
}

variable "log_group_name" {
  description = "CloudWatch log group for worker container output."
  type        = string
}

variable "log_group_arn" {
  description = "ARN of the CloudWatch log group."
  type        = string
}

variable "gvisor_release" {
  description = "gVisor release directory under https://storage.googleapis.com/gvisor/releases/release/ (e.g. latest or 20240916.0)."
  type        = string
  default     = "latest"
}

variable "tags" {
  description = "Tags merged into every taggable resource."
  type        = map(string)
  default     = {}
}
