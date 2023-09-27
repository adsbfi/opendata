# adsb.fi open data API

The base URL for adsb.fi open data API is <https://opendata.adsb.fi/api/>

Please read the terms below before using the API. By accessing the API, you agree to the terms.

Please contact us if you have commercial or higher request rate requirements.


## Public endpoints

These endpoints are publicly accessible, but we kindly ask you to support adsb.fi by setting up a receiver. The endpoints and responses are compatible with ADSBexchange v2 API.

| Endpoint | Method | Description |
| --- | --- | --- |
| /v2/hex/[hex] | GET | Returns aircraft by Mode-S hex code |
| /v2/icao/[hex] | GET | Returns aircraft by Mode-S hex code |
| /v2/callsign/[callsign] | GET | Returns aircraft by callsign |
| /v2/registration/[reg] | GET | Returns aircraft by registration |
| /v2/sqk/[squawk] | GET | Returns aircraft by squawk |
| /v2/mil | GET | Returns aircraft marked as military |
| /v2/lat/[lat]/lon/[lon]/dist/[dist] | GET | Returns aircraft within specified distance up to 250 NM |

Example usage:  
`curl https://opendata.adsb.fi/api/v2/hex/461E1A`  
`curl https://opendata.adsb.fi/api/v2/icao/461E1A,4ACA0B`  
`curl https://opendata.adsb.fi/api/v2/lat/60.3179/lon/24.9496/dist/25`


## Feeder endpoints

These endpoints are accessible for people contributing to adsb.fi by hosting a receiver. Your feeder IP address is automatically given access.

| Endpoint | Method | Description |
| --- | --- | --- |
| /v2/snapshot | GET | Returns all aircraft refreshed twice a minute |

Example usage:  
`curl https://opendata.adsb.fi/api/v2/snapshot`


## Limits
We use rate limiting to protect against abuse and misuse, and to help ensure everyone has fair access to the API.
The public endpoints are rate limited to 1 request per second, and the feeder endpoint to 1 request every 30 seconds.


## Terms
adsb.fi open data is for personal, non-commercial use only. You may not license, sell, rent, or lease any part of the data or the service. 
The data and the service are provided as-is, without any warranty. You must cite adsb.fi and include a link to our home page. We reserve the right to suspend or terminate the service or access to it. 
adsb.fi and its maintainers assume no liability for any injury, loss, or damage.
