# Remote state: S3 object + DynamoDB lock. Backend blocks cannot use variables,
# so either edit the placeholders below or override them at init time:
#
#   terraform init \
#     -backend-config="bucket=<your-state-bucket>" \
#     -backend-config="dynamodb_table=<your-lock-table>" \
#     -backend-config="region=<region>"
#
# See infra/terraform/README.md for the one-off bootstrap of the bucket/table.
terraform {
  backend "s3" {
    bucket         = "REPLACE_ME-agent-fabric-tfstate"
    key            = "agent-fabric/dev/terraform.tfstate"
    region         = "eu-west-2"
    dynamodb_table = "REPLACE_ME-agent-fabric-tflock"
    encrypt        = true
  }
}
