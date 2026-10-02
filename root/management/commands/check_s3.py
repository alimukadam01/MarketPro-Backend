"""
Probe the media bucket and say exactly which S3 permission is missing.

    python manage.py check_s3

Written because the failure mode is genuinely misleading: S3 answers HeadObject
on a key that does not exist with 403 Forbidden, not 404, unless the caller
also holds s3:ListBucket on the bucket. django-storages re-raises anything that
is not a 404, so a missing ListBucket surfaces as a 500 on upload with a
traceback that points at HeadObject and looks like the object is unreadable.

Each check runs independently so one failure does not mask the next.
"""
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Check that the configured S3 bucket is reachable and writable.'

    def handle(self, *args, **options):
        if not getattr(settings, 'USE_S3', False):
            self.stdout.write(self.style.WARNING(
                'USE_S3 is false - uploads go to MEDIA_ROOT, not S3. '
                'Nothing to check.'))
            return

        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            self.stdout.write(self.style.ERROR(
                'boto3 is not installed. pip install -r requirements.txt'))
            return

        bucket = settings.AWS_STORAGE_BUCKET_NAME
        region = settings.AWS_S3_REGION_NAME
        prefix = f"{getattr(settings, 'MEDIA_LOCATION', 'media')}/"

        self.stdout.write(f'bucket   {bucket}')
        self.stdout.write(f'region   {region}')
        self.stdout.write(f'prefix   {prefix}')
        self.stdout.write(f'endpoint {getattr(settings, "AWS_S3_ENDPOINT_URL", None)}')
        self.stdout.write('')

        client = boto3.client(
            's3',
            region_name=region,
            endpoint_url=getattr(settings, 'AWS_S3_ENDPOINT_URL', None),
        )

        session = boto3.Session()
        creds = session.get_credentials()
        if creds is not None:
            # 'env', 'shared-credentials-file', 'iam-role', 'assume-role', ...
            self.stdout.write(f'creds    found via {creds.method}')
            if creds.method == 'shared-credentials-file':
                self.stdout.write(
                    '         (~/.aws/credentials - settings.py sets no keys, '
                    'so boto3 fell back to this)')

        try:
            identity = boto3.client('sts', region_name=region).get_caller_identity()
            self.stdout.write(f'identity {identity["Arn"]}')
        except NoCredentialsError:
            self.stdout.write(self.style.ERROR(
                'No credentials found. On EC2 that means the instance has no '
                'IAM role attached; locally it means AWS_ACCESS_KEY_ID and '
                'AWS_SECRET_ACCESS_KEY are not set.'))
            return
        except ClientError as error:
            if self._is_credential_error(error):
                self.stdout.write('')
                self.stdout.write(self.style.ERROR(
                    'The credentials are not valid - AWS cannot identify the '
                    'caller, so no policy was even consulted. This is NOT an '
                    'IAM permission problem.'))
                self.stdout.write(self.style.ERROR(
                    f'  error: {self._code(error)}'))
                if creds is not None:
                    self.stdout.write(self.style.ERROR(
                        f'  they came from: {creds.method}'))
                self.stdout.write(self.style.ERROR(
                    '  Running locally there is no instance role, so set '
                    'AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env (or '
                    'fix the profile boto3 picked up above).'))
                return
            self.stdout.write(self.style.WARNING(f'identity unknown ({error})'))

        self.stdout.write('')
        ok = True
        missing = []

        # 1. ListBucket. This is the one that makes the others lie: without it,
        #    a missing key reads as 403 rather than 404.
        try:
            client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
            self.stdout.write(self.style.SUCCESS('PASS  s3:ListBucket'))
        except ClientError as error:
            ok = False
            missing.append('s3:ListBucket')
            self.stdout.write(self.style.ERROR(
                f'FAIL  s3:ListBucket - {self._code(error)}'))

        # 2. HeadObject on a key that cannot exist. 404 is the healthy answer.
        probe = f'{prefix}_check_{uuid.uuid4().hex}.txt'
        try:
            client.head_object(Bucket=bucket, Key=probe)
            self.stdout.write(self.style.WARNING(
                'odd   a random probe key already exists'))
        except ClientError as error:
            code = self._status(error)
            if code == 404:
                self.stdout.write(self.style.SUCCESS(
                    'PASS  HeadObject on a missing key returns 404'))
            elif code == 403:
                ok = False
                self.stdout.write(self.style.ERROR(
                    'FAIL  HeadObject on a missing key returns 403, not 404. '
                    'This is the upload 500. Grant s3:ListBucket on the '
                    'BUCKET arn (not the /* one).'))
            else:
                ok = False
                self.stdout.write(self.style.ERROR(
                    f'FAIL  HeadObject - {self._code(error)}'))

        # 3. PutObject / GetObject / DeleteObject, on a throwaway key.
        try:
            client.put_object(Bucket=bucket, Key=probe, Body=b'ok')
            self.stdout.write(self.style.SUCCESS('PASS  s3:PutObject'))
            try:
                body = client.get_object(Bucket=bucket, Key=probe)['Body'].read()
                if body == b'ok':
                    self.stdout.write(self.style.SUCCESS('PASS  s3:GetObject'))
                else:
                    ok = False
                    self.stdout.write(self.style.ERROR('FAIL  GetObject returned wrong bytes'))
            except ClientError as error:
                ok = False
                missing.append('s3:GetObject')
                self.stdout.write(self.style.ERROR(
                    f'FAIL  s3:GetObject - {self._code(error)}'))
            finally:
                try:
                    client.delete_object(Bucket=bucket, Key=probe)
                    self.stdout.write(self.style.SUCCESS('PASS  s3:DeleteObject'))
                except ClientError as error:
                    ok = False
                    missing.append('s3:DeleteObject')
                    self.stdout.write(self.style.ERROR(
                        f'FAIL  s3:DeleteObject - {self._code(error)} '
                        f'(leftover key: {probe})'))
        except ClientError as error:
            ok = False
            missing.append('s3:PutObject')
            self.stdout.write(self.style.ERROR(
                f'FAIL  s3:PutObject - {self._code(error)}'))

        self.stdout.write('')
        if ok:
            self.stdout.write(self.style.SUCCESS(
                'Bucket is reachable and writable. Uploads will work.'))
        else:
            self.stdout.write(self.style.ERROR(
                'Fix the IAM policy for the identity above (only if the '
                'credentials themselves were accepted). Needed: '
                's3:PutObject, s3:GetObject, s3:DeleteObject on '
                f'arn:aws:s3:::{bucket}/{prefix}* AND s3:ListBucket on '
                f'arn:aws:s3:::{bucket}'))
            if missing:
                self.stdout.write(self.style.ERROR(
                    'Explicitly missing: ' + ', '.join(sorted(set(missing)))))

    # AWS answers an unknown or disabled key with these, long before it looks
    # at any policy. Treating them as "permission denied" sends you to the IAM
    # console to fix something that is not broken.
    CREDENTIAL_ERRORS = {
        'InvalidAccessKeyId', 'InvalidClientTokenId', 'SignatureDoesNotMatch',
        'ExpiredToken', 'TokenRefreshRequired', 'UnrecognizedClientException',
        'AuthFailure', 'InvalidToken',
    }

    @classmethod
    def _is_credential_error(cls, error):
        return error.response.get('Error', {}).get('Code') in cls.CREDENTIAL_ERRORS

    @staticmethod
    def _status(error):
        return error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')

    @staticmethod
    def _code(error):
        meta = error.response.get('ResponseMetadata', {})
        return (f"{meta.get('HTTPStatusCode')} "
                f"{error.response.get('Error', {}).get('Code', '')}").strip()
