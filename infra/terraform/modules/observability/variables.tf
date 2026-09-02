variable "name_prefix" {
  description = "Prefix applied to resource names."
  type        = string
}

variable "retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags merged into every taggable resource."
  type        = map(string)
  default     = {}
}
