## Google Trust Services ACME account (internal)

this is like let's encrypt but with higher rate limits tied to your GCP account (but still free).

https://docs.cloud.google.com/certificate-manager/docs/public-ca-tutorial
https://docs.cloud.google.com/certificate-manager/docs/quotas

prod server is at: https://dv.acme-v02.api.pki.goog/directory

brew install gcloud-cli

gcloud-init to genint project

`gcloud projects create openhost-tls-certs-1`
`gcloud config set project openhost-tls-certs-1`

`gcloud publicca external-account-keys create`

brew install certbot

if you have an existing account, `sudo rm /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog` to clear.

sudo certbot register \
    --email "me@example.com" \
    --no-eff-email \
    --server "https://dv.acme-v02.api.pki.goog/directory" \
    --eab-kid "(from previous step)" \
    --eab-hmac-key "(from previous step)"

it does not seem that the email becomes public. sudo is just needed because certbot writes its config to /etc/letsencrypt. this is the GCP prod keyserver.

grab the key from /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog/directory/[key id?]/private_key.json

put that in certbot_private_key.json in ansible secrets (this is now kept in 1password).

to revoke the keys, you have to delete the whole project. to reset rate limits, you can make a new project.
