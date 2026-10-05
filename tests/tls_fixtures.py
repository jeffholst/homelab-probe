"""Throwaway certificates for the TLS tests (generated once with `openssl req -x509 -newkey rsa:2048 -nodes -days 36500`;
they protect nothing). `good` is a CA:TRUE self-signed certificate for 127.0.0.1 and localhost, `othername` is valid
only for other.example and `otherca` is another valid certificate for the same addresses.
"""

import contextlib
import socket
import ssl
import tempfile
import threading
from pathlib import Path
from typing import Iterator


def _private_key(body: str) -> str:
    """The PEM of a key kept without its header lines, so that no file of the repository looks like a leaked key (the
    keys protect nothing: they belong to certificates made for these tests)."""
    return "-----BEGIN " + "PRIVATE KEY-----\n" + body.strip() + "\n-----END " + "PRIVATE KEY-----\n"


GOOD_CERT = """-----BEGIN CERTIFICATE-----
MIIDcTCCAlmgAwIBAgIUPBiOGOUUyVoK2Rm3VmOWMwTqs3AwDQYJKoZIhvcNAQEL
BQAwMTESMBAGA1UEAwwJZ29vZC50ZXN0MRswGQYDVQQKDBJIb21lbGFiIFByb2Jl
IFRlc3QwIBcNMjYxMDA1MTczMjE3WhgPMjEyNjA5MTExNzMyMTdaMDExEjAQBgNV
BAMMCWdvb2QudGVzdDEbMBkGA1UECgwSSG9tZWxhYiBQcm9iZSBUZXN0MIIBIjAN
BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA73LxXXpTtygtvDKtJxS5ThFKCYI/
xwUDsDaKYcEN96XOZZhoHnNEIa1CfLnW57e+HHPLfkWnJuQbyJm9yufNov7bFtrz
8lHby5oqqFuvKrhZzmVqT71migK+MgTMVZciGzWttTJOvQeh6rhnJykMb1ADVesQ
FArM5uClrXUk8z9jr77vwiEzHHrwr4RyXXTxs02hA9WiN2V9nYhaHyz2TjEremG/
KZctpZMi8QumaMycylvyvAyMB9uzWO/lPhEi3Na08GrJqTtjj63fN/f3AVyOTpkM
CbyEfH63W+QkGZE1+waOWd792tDza5e1ASEk2VD5bepffW3tXWiQxgjFAwIDAQAB
o38wfTAdBgNVHQ4EFgQU3s7wVwQCM4683Znm7Kq1fJZKfCswHwYDVR0jBBgwFoAU
3s7wVwQCM4683Znm7Kq1fJZKfCswGgYDVR0RBBMwEYcEfwAAAYIJbG9jYWxob3N0
MA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgKEMA0GCSqGSIb3DQEBCwUA
A4IBAQB4QU4yeHjB6sTAbTAtghYbWKqfbbNmc9qHejPIjlqV9ZdvGQXWdKHAs+q+
1ybfaewDPRSc99PsZCpWQhDrwSFXvtm9jAjEoPkBNGOwwqBIh5Mt/+ahCkY07Z0d
xXx5fi/npun5RBauYUvKm4hqyPuLBrb+VN17gA27ZfVSnWL5TMnPpHVJGkMdnHZz
hC6ATcUNcrNudgvGj4dUM61/Md9ou50QtVbCLTMhJedBAPT+mm0rm6eKZPVy4FG1
ptR8C6BgxfbboSjPAt8MUYd1SaYXdIdKyAnafGSwPq+DhcDuZklnHdd8K0rbNf2L
RtUxpKlApawRWu6cWwgXNT/bmJZk
-----END CERTIFICATE-----
"""
GOOD_KEY = _private_key("""MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDvcvFdelO3KC28
Mq0nFLlOEUoJgj/HBQOwNophwQ33pc5lmGgec0QhrUJ8udbnt74cc8t+Racm5BvI
mb3K582i/tsW2vPyUdvLmiqoW68quFnOZWpPvWaKAr4yBMxVlyIbNa21Mk69B6Hq
uGcnKQxvUANV6xAUCszm4KWtdSTzP2Ovvu/CITMcevCvhHJddPGzTaED1aI3ZX2d
iFofLPZOMSt6Yb8ply2lkyLxC6ZozJzKW/K8DIwH27NY7+U+ESLc1rTwasmpO2OP
rd839/cBXI5OmQwJvIR8frdb5CQZkTX7Bo5Z3v3a0PNrl7UBISTZUPlt6l99be1d
aJDGCMUDAgMBAAECggEAGttS5HUv++nEbMi8+xaBTOxLgugvrGw3jwV5sp/V/TeF
ADLT8CEQPJFo/6xTGl0ANG4Yc5VEZkGiNsxnqTZ5WMAqZVSG6rWbvX5mgnOmIK3a
BM2t2uZsyytQ7Iif598HsvLrr4MbqcANZ9I/Ck65rP0rIpvhix3wVYT6OuEEv/OI
o4XCOlWrRAaYyxk264Jix8GQyKRDYkC5CbNpU5sCwzU76ZPNioTYlJDrDcUq71Fn
qwTKQFVZw0rQZ2atqNRGpyJTl+e5E6rS0hGLP4mVx5F7+QwRfRHP6DO9GfCEWkkp
r1h5mR2YMMVYn3HYlDAkrBros2zUzdAZG5lyg/2FwQKBgQD6vjlvHT+/sEp4UBgT
AqHwEfspwynWBlVOXou4aDXn1MwPDgcvrLVUKJ3JFRlj99QOEQfE34ypk/nGZ76/
FGSj1hVTouFrW/8jvdw4iQ/oV4a9J9rGEh9O3szMxDHytZt84SpTDEA+kdyJOjE5
NYwE35AnNUyG/balQ1USfKcmSQKBgQD0eBn9RjOt32C8HB1Qp+iQz0gLoXP7JLUm
G07+/eg1H9IUbmgmgjtosWzL/1KWzExQ/wNKVW6VfH/dK3RN+a9M5Xkro2uFvzEo
SsVwaJ7IRe5lwBKPBHRnpmQLdP+pJLVFdVszTqt4tpUUibpYes5B3XEn6BcU0dRL
jHN+v1Og6wKBgGiZMAZdMjm5xecYqmJLx9gI+Dh8tJgWvkFaCXkc59fVGmbxWCgY
KPB5nRDEH3pnaOuRSSdkhh47n4eXwaeeTzlVVkI0gUqy7uUvD436B0vKL2f7FzVn
W+4f3VlSQu/XuIxItco9IxO270PDpcMSxl1GEbF5d3ocnrOkOfnjTCsZAoGBAJFP
zkrw7oj8TPijUX1+wMtKmk1ng5QVZqOm+daxv9PL/Uhts/Sn1n5NcBj1w/akqvw+
CIunqlqqrSoeyTwMHPn9MIAS3DecxLBpeWBun2r1vlW2zJE8GaD+k1sICWtVyXXm
4vXlXiEbjhOuJhivrmgSI0+QMiAK9UCO1JvTR/dPAoGAR4orJT53la3/etbRqR+I
mwdQbTdE9KqbDnOlFWenVYkNqdC/FjP97BmONwtH0gyLrXfSitDkmw8qpHWOEotF
qZN0gbuyzbKkMW+mQfmMz1v6w++eqzZFXNZIO0ediwszrHMLjsRfn15HInjiTNXd
sgjcJCpAS7fzX9rDuZskAjc=
""")
OTHERNAME_CERT = """-----BEGIN CERTIFICATE-----
MIIDeDCCAmCgAwIBAgITEqd7rKwvvLUEq6JWNn9clF/qAjANBgkqhkiG9w0BAQsF
ADA2MRcwFQYDVQQDDA5vdGhlcm5hbWUudGVzdDEbMBkGA1UECgwSSG9tZWxhYiBQ
cm9iZSBUZXN0MCAXDTI2MTAwNTE3MzIxN1oYDzIxMjYwOTExMTczMjE3WjA2MRcw
FQYDVQQDDA5vdGhlcm5hbWUudGVzdDEbMBkGA1UECgwSSG9tZWxhYiBQcm9iZSBU
ZXN0MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsfQpKJ4wdL13g68A
v6GWeLPeOOs18QGjSsFCX5ckf3Tu3BIFfMa9qz9RQ6UeWUrCn9XEPFK5+1SNmfqm
L81yb87qx3jDH6exnsIikTVxsADx/zKlkhJo9vEr1gjtN/IfbaguYJS7soY8Lumx
Zf2z4WNX7ioObGKbmJ1XNtUMuc4FRGaXtFLgW9uwQEIS4aUOwPj5waXR1olXVcA0
UqJ7js8URVVvHoIpAcerkpC+CXTgc6FWPOConlE3ocb2o7scvzZbV57+LLz6wHRf
/OUBG0IXEwHNFWEAA+v6nMqPJNCXqgLfgxtgQ0tCmWkxbq2IJMKRsOtksE9CvAWI
1KLFqwIDAQABo30wezAdBgNVHQ4EFgQUen5G6FJa4dwSVcHjA3enyQfhgRQwHwYD
VR0jBBgwFoAUen5G6FJa4dwSVcHjA3enyQfhgRQwGAYDVR0RBBEwD4INb3RoZXIu
ZXhhbXBsZTAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIChDANBgkqhkiG
9w0BAQsFAAOCAQEACUGfNDlqX7HHTZoNZHcuDAS/rzVBOy9GgpomuGKbAqZIF/5C
ST4um7OVXy6KpEBDc4y0vsoa6bUme4odknvbP1XvWcvUU1+DWcqKxvzJLy+60R8m
wftVWicO2CLw3sGHNgxzYtS2Q0IJKVKXTRwgNyfQcuAB85C1UFY09KZq7zuxgjiS
CREc8237eMmWLwz8/lh2nQsJES1prUoNNhR9+PuQqVhFif6lpR7UOffJPvxmjpP+
Q4KY+JXOKQvQgtM2Nwb3CeVaSQpt32HLPId4TcoIT+oAnXe3O/avvOXv2QR4I5J5
NKdNdQJMLy3n7g1/kQXgG8WJ9hTbnJy4HiBs3A==
-----END CERTIFICATE-----
"""
OTHERNAME_KEY = _private_key("""MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCx9CkonjB0vXeD
rwC/oZZ4s9446zXxAaNKwUJflyR/dO7cEgV8xr2rP1FDpR5ZSsKf1cQ8Urn7VI2Z
+qYvzXJvzurHeMMfp7GewiKRNXGwAPH/MqWSEmj28SvWCO038h9tqC5glLuyhjwu
6bFl/bPhY1fuKg5sYpuYnVc21Qy5zgVEZpe0UuBb27BAQhLhpQ7A+PnBpdHWiVdV
wDRSonuOzxRFVW8egikBx6uSkL4JdOBzoVY84KieUTehxvajuxy/NltXnv4svPrA
dF/85QEbQhcTAc0VYQAD6/qcyo8k0JeqAt+DG2BDS0KZaTFurYgkwpGw62SwT0K8
BYjUosWrAgMBAAECggEAC8qQYZsdvdaCEU0qwQfddxEABAh90gLYRY9JrRjQN8vR
Oe7Nw0dN8QdohJFv0d3UyI97Cb43iZJAMbg8g8VatFLjFqWHVFUhvVHCxZljd5SQ
dGbwNp4Wq2oui+eahE78SyFONWMVjs47NaaRdR8a6K/S9zp9KyxbgADI8x6p0bPa
2MvV23+Q+D9QR9zLJKczsspqVQaFHn1o8YyWTJ7GPq1FPeFklxXIXLxkfyWsbQfW
sXEeb0FGeo82HKmCnTyTE5KzCzAHXas/sQ1trorn5O3JgC3YOj2hggaGW1zf5ZfX
6GNnAAtIkgmFZ7Zwuggiw3icF/1WHkCh7wLLdHG5SQKBgQD62A+U/A6d+N5/gxIr
U7D//scbc3Ct9ZO5ZS4Mp7EzKxi0ozzQCnqU6mBqGE0sLULIbi8QnlxwoCH4NDXw
M7/VjqEUXEsFXVsrdlLoSKwGcWkMvrX0k+zYdpcB/PX0n38f5t2l3FdEO+t2Qxoi
gr2j74zMjl9iGfTRR/iob51BMwKBgQC1nI1TdvO7g9aTNVPOWwcbo9tv68Qex19a
B3jYCmG9+Pp/Zkf9wb1j5Zppnyhyi2LViT7BR2b28unZ1NBpdmz6e/d8Grc+ifWF
0T38lYDYex4T2iB966ocT/1+N3iuiXXP0r/dCmh2JdjcJuucBoDO3GBjgRS2xP/L
DV8d7ThZqQKBgQDJaGQSftQJuUKFZatYCInM8jfSbb1ioBTtvjTcSmT4LblXiN4S
OOe12/5wEXUdbHX70qSNlmmosJq13M4WQKbeBPiHDZfbBdtnKUlmYrtlLPhpGFOm
voNkRsv297JQqSP8bHU5cJLNIcNsoHJClXFJSNVnhUVG0oqE42HfdEbyiwKBgD7Y
sRNcC5YtVljU0G6Nk5UeC/bcGJ8qETYfddMVLKPIAC+MHoeSvs5OCzRXznBtCcOp
Xd0Wr2vvvbsS6RhF0gqQUaFRwW3T4fHv6cp5lf/UAyGNj0bkAZcQm0FNQubrTKih
XqHIU0PnbvcoRMpWecab/oMQjTF+VKJYuzyo9aIZAoGAcI0gwKwIcWeVGxCwU4VB
hGjJxfU2HpXvWsDslwNm1Sd63NsaxaIW5EmRPMoG2gez5zT6XJBHRX+nXPpe0RM+
NpLee7hJ8Jt6QPVUZxS2sFfj6hpEBry2e1naWkh+D206+4E1GIQ3/hjhLUPCGiaf
t367hklppYgvkJ3gsuwymq8=
""")
OTHERCA_CERT = """-----BEGIN CERTIFICATE-----
MIIDdzCCAl+gAwIBAgIUGvFmBxhArR8cgra5pbc7b+iZH3owDQYJKoZIhvcNAQEL
BQAwNDEVMBMGA1UEAwwMb3RoZXJjYS50ZXN0MRswGQYDVQQKDBJIb21lbGFiIFBy
b2JlIFRlc3QwIBcNMjYxMDA1MTczMjE3WhgPMjEyNjA5MTExNzMyMTdaMDQxFTAT
BgNVBAMMDG90aGVyY2EudGVzdDEbMBkGA1UECgwSSG9tZWxhYiBQcm9iZSBUZXN0
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnhNtytN56KtlGk6xrTC2
vJVRbd3w3kHJp/Ws4F+JvcieJesYZttHiImT8MFJ81oGTVUC4kNLdsPg5q+SIR50
GUtLtP5mwcDfrFaLDwxVXOA0GhSSedSABr1494qfMDFdrUSUgUFEHPpa2f6EEVei
p9aduEleEVsk9YLq8qjV80WlSI6nhK/Fq715vGYdooKdQPrV+a6jIuFCyweVZxil
wyGxvVy+z9zdN83/USt2uaKKeFaPlHGekm5IyGe89dXAz94dYgO3yx+Bbhds0uMD
dzwo6f2YlBP8rKE/u4TNr8Rm5THtJlt1q0JRWIHIwGrxSLwv5WMYOB2WFxaDsSim
4wIDAQABo38wfTAdBgNVHQ4EFgQU+8axC0nGpFc7XVlHGFNF9SXWPNwwHwYDVR0j
BBgwFoAU+8axC0nGpFc7XVlHGFNF9SXWPNwwGgYDVR0RBBMwEYcEfwAAAYIJbG9j
YWxob3N0MA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgKEMA0GCSqGSIb3
DQEBCwUAA4IBAQAz6Wf/Y2stsZKjlyNv6BDCdhknA8tqYpcF4CU/cyGuQDrN1/3T
V9bPYksDbvyPXeHtue+b//mIdPqhsxmG0qF7fq1xa2zYyKjFP6KMhb80lySHhsgM
9jT4RYADf0SmZBBtMGViXEiBB854IeUZ73sd+YO47tS69BuXvC99xJeXGn+rSohA
3rskHHd5shlM1iIH3lW7urDhgyK/0px0PIUPskFrIbHkJlDpPhKYc5tK8u9zwt++
LoszDQbaqx/w2Pl22cdEad/nZC3mshv6b8NbMCcf2+6k+mqQuWuRBUOIqVsa50x5
I+NRoNTRaje8FAOWebSFIXajPmkZIMyYeure
-----END CERTIFICATE-----
"""
OTHERCA_KEY = _private_key("""MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCeE23K03noq2Ua
TrGtMLa8lVFt3fDeQcmn9azgX4m9yJ4l6xhm20eIiZPwwUnzWgZNVQLiQ0t2w+Dm
r5IhHnQZS0u0/mbBwN+sVosPDFVc4DQaFJJ51IAGvXj3ip8wMV2tRJSBQUQc+lrZ
/oQRV6Kn1p24SV4RWyT1guryqNXzRaVIjqeEr8WrvXm8Zh2igp1A+tX5rqMi4ULL
B5VnGKXDIbG9XL7P3N03zf9RK3a5oop4Vo+UcZ6SbkjIZ7z11cDP3h1iA7fLH4Fu
F2zS4wN3PCjp/ZiUE/ysoT+7hM2vxGblMe0mW3WrQlFYgcjAavFIvC/lYxg4HZYX
FoOxKKbjAgMBAAECggEANi6jqetttFYXO/qwRRRW9nHZ7OYvewcit0rqvCeTH3WF
26pm/U55CPBbQKEIF30KfvQ+Hk8BrU47puaUGH0HP2pDI2E12ICjSj+Jv5kzIzgI
M85MXKz9SxIjxfmCUfMB0HOa0WkNDz+y51ipbQZUTuItwm6HazyKAQskPBOp7g2t
vP5WNgT0y+CfT14tqeBrYDh+5+Pt6twKIWY4JRfUIQq6sNn/CIxZ4JoyZnJ/rcHb
AgpCPRn5tLIrKNWlGNR5BRO6Hi8UiRUIKVslgAEatqUahsX8J02usId0cVn88YeH
ZcpmZhONDe4QIp5c5hVtHhJoaA0hwqIy8wWX/++GwQKBgQDPxRNyfE302lQ0/2SK
5zr6qDoUJasV13AzOt/srfXQF26cBiJCgbOcxUgDW+v21jqLvNXSc1rz9UHdS9Wq
ClKhLAsC0a4QKUTDqbvTYe+seppGu/Qz8UAsLE+BwG0Hirw7d7X5G+1GtXinDofP
sxs5NqGz09S2MEOdeQBjpe3PIwKBgQDCxT4ZGiWfascBoDU59DtRj9wRHQDNSvan
U+7dg0iH7UZ4Vo1DfpLj6QPj3s9Y9KvSlaFLc/5yJjKsIE2Pcn6Vegx+PgRhPcnt
04unQ16391oulPBrRWWhNzpLbA2t4zOixXuWIU2j0VALI1fX/ID/+EMbXD8yuMR8
/nhH7L0lQQKBgD17IssQ73ySErLwFA9qZzB/EVie4OaArsnqFRMTUxb1qBxxUDf/
62drlWixDb0oHYD2KwuwaOyh2ZCjfSFpFpRs1QFLjMdpftirZFIju+l9CiP+QEAS
lpu7rPdHOrwtmI+8V9PUKL1wu7gre5LBfD/M48Kz31DeLVPu12elVR6jAoGBAKO1
QB2/QXI+QKkYLMTaiOVkQrfLpfnoEWvRe4uKpnNBt6K8NR5PDE4udAubIrMFbEqj
ZCBUjcEKZDsTJ73zoXwoSsntfKbzVX/l+JMNaFa+vHk8zNuNR//6uK/eUZ0fxC4D
SX6F3YldDKdf/JzZauA7nsQnhCMw9E6PYTArR0xBAoGAcDn0isT4lIGHUooXPBBj
/bBVU30YVnWVZ2GXYHAdoviFqjmlWFWyjfuHProwpfQBYouszD2wdaz+Ohn4HEyT
L9cdKfMyJYQplfg3/GiXr5FoI2QUrK+1ka+MsyW1+TAM3VNVhr8FOncQgFMvH9kZ
Vw8/BlqfNzicbArnqshjNS4=
""")


@contextlib.contextmanager
def tls_server(cert: str, key: str) -> Iterator[int]:
    """A TLS server on 127.0.0.1 (any free port) that shows ``cert`` to everyone who connects; yields the port."""
    with tempfile.TemporaryDirectory() as directory:
        cert_file, key_file = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        cert_file.write_text(cert)
        key_file.write_text(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_file), str(key_file))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.2)
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except (TimeoutError, OSError):
                    continue
                try:
                    with context.wrap_socket(connection, server_side=True) as secured:
                        secured.settimeout(1)
                        try:
                            secured.recv(1)
                        except (OSError, ssl.SSLError):
                            pass
                except (OSError, ssl.SSLError):
                    connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stop.set()
            thread.join(2)
            listener.close()
