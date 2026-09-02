variable "bucket_name" {
  description = "Globally unique S3 bucket name for run logs."
  type        = string
}

variable "expiration_days" {
  description = "Objects are expired after this many days."
  type        = number
  default     = 30
}

variable "force_destroy" {
  description = "Delete all objects on destroy (true for throwaway dev)."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags merged into every taggable resource."
  type        = map(string)
  default     = {}
}
