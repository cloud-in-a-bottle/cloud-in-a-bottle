
## Specific Design

- eventually this will become a new DID method, so let's keep close to that setup
- i want a new hybrid method - public key / DID is the source of truth, but the domain is the human-readable advisory ID. anytime you really need to ensure the domain shortname is valid, you’ll hit the domain and verify it still controls its private key. and the user can refer to themselves by their domain - eg to create a login - with the assumption that if the user thinks they control their domain, it’s probably safe to assume that is true for the next ~5 mins or whatever. so you can hit their domain and fetch their public key.
- openhost-specific context
  - each user has a Cloud in a Bottle space, accessable at a specific domain (user.host.imbue.com or whatever).
  - their userspaces all run a copy of an identity provider service that handles to the challenges+flows discussed in this doc.
  - a Cloud in a Bottle app (owned/hosted by user A) can give access to other Cloud in a Bottle users (user B) by the following flow
    - user A adds/invites user B by their Cloud in a Bottle URL (userb.host.imbue.com)
    - the app immediately contacts user B's server to get their full public key / public identity
    - later, user B visits the app hosted in user A's space. the app says something like "please login with your Cloud in a Bottle identity". user B clicks this button, and it routes them to a path in their own space (via the my.host.imbue.com route, which for user B redirects to userb.host.imbue.com). this may be something like `/identity-challenge/?source=app.usera.host.imbue.com`. probably with some type of token thing?
    - the identity provider server in user B's space receives this request and shows the user a page like "you're getting a login request from app.usera.host.imbue.com, would you like to use your Cloud in a Bottle identity to login?". of course this only works if user B is logged into their own space.
    - user B clicks yes, and this redirects back to `app.usera.host.imbue.com/identity-response` with some sort of cryptographic token proving that they do indeed control their private key (which is stored in their space).



## General context (copied from another doc)

- i want a way to do things like give permissions to an app to specific other Cloud in a Bottle users. so they are authed if they’re logged into their instance, i guess?
- eg for the project management system - i need a way to invite other users, and i don’t want them to have to make accounts specifically for this. they should be able to use their VM as a SSO provider, maybe?
    - i guess how this would work:
        - you have some way of uniquely+securely identifying another Cloud in a Bottle user, probably via a PGP public key or similar.
            - this could come from a “contacts”/”friends” app/service in your VM, so you can add known people once and then reference them easily later.
        - then you say “i will share this with user xyz”
        - when user xyz accesses your space/app, the app is like “tell me who you are”.
            - the user can maybe manually enter their PGP identity?
            - but better is some way that we redirect to my.openhost.imbue.com/sso, which redirects to the user’s space, and is like “hey do you wanna log in to this app”. if you say yes, then it’ll redirect back with whatever crypographic challenge response is needed to provide the identity.
- existing implementations of this / standards. generally called “decentralized/federated identity”
    - indieauth
        - your domain name is your identity
        - you go to a site. it asks you to sign in. you enter just your domain name. the site hits a standard endpoint on your domain to check if it supports auth and get any needed details (versions or whatever). it then redirects you to a path at your domain, where you host an auth provider server. your server makes sure you’re logged in to it (via whatever method it likes) is like “hey i got an auth request from this app, do you want to approve it”. if you approve, it redirects you back to the original site with a signed token confirming that you gave auth.
    - webfinger
        - sorta like a step between something like indieauth. user enters their email address, and it goes to the domain and asks for some info about that user. that might include various links, one of which is the path to their identity provider endpoint (eg indeauth or whatever). seems kinda redundant.
    - solid (the datapods thing) also has a decentralized auth
    - w3-standard Decentralized Identifiers (DID)
        - “did:method_name:method_id”
        - method name determines the way that a method_id is mapped to a DID document. examples
            - web: implements domain-based id. “did:web:my_domain.com”
            - key: just dump your public key in the ID field. DID document is directly derived from the key, no lookup needed. no way to rotate the key
                - could you use a subkey per service that can be revoked? not sure how revoking works. why would you need to rotate it?
                - maybe you keep a list of subkeys you’ve issued, and your signing server can choose not to sign ones that you’ve revoked.
            - plc: the bluesky AT protocol. centralized DID document directory (that you can update using a login at this site?). no real plan to make this decentralized?
            - some blockchain-based DID databases.
    - bluesky’s AT Protocol
        - ultimate source of truth is DID with plc protocol - basically just a centralized DID directory that they control
        - and then the ability to pair a DID with a domain name to be used as a short-name. your domain hosts a static route saying “yes i am this DID id” and the DID document says “yes i can be identified by this domain name”. both must agree for the short-name to be used.
    - OIDC Self-Issued OpenID Provider (SIOP)
    - the core PGP signing flow is pretty clear, but the web protocols for it are not so much.
- “your identity is your domain” is interesting. kinda like the ACME TLS signing. prove you control your domain, and you can be identified just by your domain name.
    - i don’t love that you have to pay (DNS registration) to maintain your identity tho. and you can accidentally lose access to this.
    - or maybe you just want to change your domain name? that gets awk
    - it is nice tho because we do want to know where someone’s domain lives, so we can contact them eg to verify their identity (or for any cross-user stuff). so then we don’t have to think about how to keep our knowledge of where they live up to date.
    - this is sorta similar to email-based auth (widely used nowadays at least for backups) - if you lose your email, you lose your identity. but most people use eg gmail for email, which is free and relatively secure. if you self-host your email, though, you do have the same problem.
- “Human-readable, decentralized, and persistent — you get to pick two. Domains give you the first two. Cryptographic identifiers give you the last two.”
    - i guess a gmail and email-based auth gives you “human readable” and “persistant” (ish).
    - Zooko's Triangle — "human-meaningful, decentralized, secure — pick any two.”
- i feel like you could do a hybrid - public key / DID is the source of truth, but the domain is the human-readable advisory ID. anytime you really need to ensure the domain shortname is valid, you’ll hit the domain and verify it still controls its private key. and the user can refer to themselves by their domain - eg to create a login - with the assumption that if the user thinks they control their domain, it’s probably safe to assume that is true for the next ~5 mins or whatever. so you can hit their domain and fetch their public key.
- i think we could also use a blockchain method for this, and other things that need a light decentralized db? have each VM run an instance of the miner. i guess there’s a question of how to prevent a DDOS attack if there’s no payments for transactions. surely there’s something we could do to address that tho.
- is there some way to integrate passkeys / hardware-based auth into this in a useful way?
