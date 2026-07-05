# Object Store Contract (ADR-0003)

## Promise
Every cloud implementation provides encrypted object storage for Zero Trust Advisor Agent:
- Server-side encryption (SSE-S3/SSE-KMS or equivalent)
- Versioning enabled for audit trail
- Lifecycle policies for cost management
- Cross-region replication for DR (optional)
- Pre-signed URLs for secure temporary access

## Interface
| Operation          | Input                      | Output            |
|-------------------|----------------------------|-------------------|
| `upload_object`   | bucket, key, body, metadata| object_url        |
| `download_object` | bucket, key               | body, metadata    |
| `list_objects`    | bucket, prefix             | object_keys[]     |
| `delete_object`   | bucket, key               | success           |
| `presign_url`     | bucket, key, ttl_seconds  | signed_url        |

## Implementors
- `modules/appops/object-store/aws-s3/`
- `modules/appops/object-store/azure-blob/`
- `modules/appops/object-store/gcs/`
