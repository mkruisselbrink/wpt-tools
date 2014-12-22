import functools
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

class OpenSSL(object):
    def __init__(self, logger, binary, base_path, conf_path, hosts):
        """Context manager for interacting with OpenSSL.
        Creates a config file for the duration of the context.

        :param logger: stdlib logger or python structured logger
        :param binary: path to openssl binary
        :param base_path: path for storing certificates
        :param conf_path: path for storing configuration data
        :param hosts: list of hosts to include in configuration (or None if not
                      generating host certificates)"""

        self.base_path = base_path
        self.binary = binary
        self.conf_path = conf_path
        self.logger = logger
        self.proc = None
        self.cmd = []
        self.hosts = hosts

    def __enter__(self):
        with open(self.conf_path, "w") as f:
            f.write(get_config(self.base_path, self.hosts))
        return self

    def __exit__(self, *args, **kwargs):
        os.unlink(self.conf_path)

    def log(self, line):
        if hasattr(self.logger, "process_output"):
            self.logger.process_output(self.proc.pid if self.proc is not None else None,
                                       line.decode("utf8", "replace"),
                                       command=" ".join(self.cmd))
        else:
            self.logger.debug(line)

    def __call__(self, cmd, *args, **kwargs):
        """Run a command using OpenSSL in the current context.

        :param cmd: The openssl subcommand to run
        :param *args: Additional arguments to pass to the command
        """
        self.cmd = [self.binary, cmd]
        if cmd != "x509":
            self.cmd += ["-config", self.conf_path]
        self.cmd += list(args)
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, stderr = self.proc.communicate()
        self.log(stdout)
        if self.proc.returncode != 0:
            raise subprocess.CalledProcessError(self.proc.returncode, self.cmd,
                                                output=stdout)

        self.cmd = []
        self.proc = None
        return stdout


def make_subject(common_name,
                 country=None,
                 state=None,
                 locality=None,
                 organization=None,
                 organization_unit=None):
    args = [("country", "C"),
            ("state", "ST"),
            ("locality", "L"),
            ("organization", "O"),
            ("organization_unit", "OU"),
            ("common_name", "CN")]

    rv = []

    for var, key in args:
        value = locals()[var]
        if value is not None:
            rv.append("/%s=%s" % (key, value.replace("/", "\\/")))

    return "".join(rv)

def make_alt_names(hosts):
    rv = []
    for name in hosts:
        rv.append("DNS:%s" % name)
    return ",".join(rv)

def get_config(root_dir, hosts, duration=30):
    if hosts is None:
        san_line = ""
    else:
        san_line = "subjectAltName = %s" % make_alt_names(hosts)

    rv = """[ ca ]
default_ca = CA_default

[ CA_default ]
dir = %(root_dir)s
certs = $dir
new_certs_dir = $certs
crl_dir = $dir/crl
database = $dir/index.txt
private_key = $dir/cakey.pem
certificate = $dir/cacert.pem
serial = $dir/serial
crldir = $dir/crl
crlnumber = $dir/crlnumber
crl = $crldir/crl.pem
RANDFILE = $dir/private/.rand
x509_extensions = usr_cert
name_opt        = ca_default
cert_opt        = ca_default
default_days = %(duration)d
default_crl_days = 1
default_md = sha256
preserve = no
policy = policy_anything
copy_extensions = copy

[ policy_anything ]
countryName = optional
stateOrProvinceName = optional
localityName = optional
organizationName = optional
organizationalUnitName = optional
commonName = supplied
emailAddress = optional

[ req ]
default_bits = 2048
default_keyfile  = privkey.pem
distinguished_name = req_distinguished_name
attributes = req_attributes
x509_extensions = v3_ca

# Passwords for private keys if not present they will be prompted for
# input_password = secret
# output_password = secret
string_mask = utf8only
req_extensions = v3_req

[ req_distinguished_name ]
countryName = Country Name (2 letter code)
countryName_default = AU
countryName_min = 2
countryName_max = 2
stateOrProvinceName = State or Province Name (full name)
stateOrProvinceName_default =
localityName = Locality Name (eg, city)
0.organizationName = Organization Name
0.organizationName_default = Web Platform Tests
organizationalUnitName = Organizational Unit Name (eg, section)
#organizationalUnitName_default =
commonName = Common Name (e.g. server FQDN or YOUR name)
commonName_max = 64
emailAddress = Email Address
emailAddress_max = 64

[ req_attributes ]

[ usr_cert ]
basicConstraints=CA:false
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer

[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
%(san_line)s

[ v3_ca ]
basicConstraints = CA:true
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always,issuer:always
keyUsage = keyCertSign
""" % {"root_dir": root_dir,
       "san_line": san_line,
       "duration": duration}

    return rv

class OpenSSLEnvironment(object):
    ssl_enabled = True

    def __init__(self, logger, openssl_binary="openssl", base_path=None,
                 password="web-platform-tests", force_regenerate=False):
        """SSL environment that creates a local CA and host certificate using OpenSSL.

        By default this will look in base_path for existing certificates that are still
        valid and only create new certificates if there aren't any. This behaviour can
        be adjusted using the force_regenerate option.

        :param logger: a stdlib logging compatible logger or mozlog structured logger
        :param openssl_binary: Path to the OpenSSL binary
        :param base_path: Path in which certificates will be stored. If None, a temporary
                          directory will be used and removed when the server shuts down
        :param password: Password to use
        :param force_regenerate: Always create a new certificate even if one already exists.
        """
        self.logger = logger

        self.temporary = False
        if base_path is None:
            base_path = tempfile.mkdtemp()
            self.temporary = True

        self.base_path = os.path.abspath(base_path)
        self.password = password
        self.force_regenerate = force_regenerate

        self.path = None
        self.binary = openssl_binary
        self.openssl = None

        self._ca_cert_path = None
        self._ca_key_path = None
        self.host_certificates = {}

    def __enter__(self):
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path)

        path = functools.partial(os.path.join, self.base_path)

        with open(path("index.txt"), "w"):
            pass
        with open(path("serial"), "w") as f:
            f.write("01")

        self.path = path

        return self

    def __exit__(self, *args, **kwargs):
        if self.temporary:
            shutil.rmtree(self.base_path)

    def _config_openssl(self, hosts):
        conf_path = self.path("openssl.cfg")
        return OpenSSL(self.logger, self.binary, self.base_path, conf_path, hosts)

    def ca_cert_path(self):
        """Get the path to the CA certificate file, generating a
        new one if needed"""
        if self._ca_cert_path is None and not self.force_regenerate:
            self._load_ca_cert()
        if self._ca_cert_path is None:
            self._generate_ca()
        return self._ca_cert_path

    def _load_ca_cert(self):
        key_path = self.path("cakey.pem")
        cert_path = self.path("cacert.pem")

        if self.check_key_cert(key_path, cert_path, None):
            self.logger.info("Using existing CA cert")
            self._ca_key_path, self._ca_cert_path = key_path, cert_path

    def check_key_cert(self, key_path, cert_path, hosts):
        """Check that a key and cert file exist and are valid"""
        if not os.path.exists(key_path) or not os.path.exists(cert_path):
            return False

        with self._config_openssl(hosts) as openssl:
            end_date_str = openssl("x509",
                                   "-noout",
                                   "-enddate",
                                   "-in", cert_path).split("=", 1)[1].strip()
            # Not sure if this works in other locales
            end_date = datetime.strptime(end_date_str, "%b %d %H:%M:%S %Y %Z")
            # Should have some buffer here e.g. 1 hr
            if end_date < datetime.now():
                return False

        #TODO: check the key actually signed the cert.
        return True

    def _generate_ca(self):
        path = self.path
        self.logger.info("Generating new CA in %s" % self.base_path)

        key_path = path("cakey.pem")
        req_path = path("careq.pem")
        cert_path = path("cacert.pem")

        with self._config_openssl(None) as openssl:
            openssl("req",
                    "-batch",
                    "-new",
                    "-newkey", "rsa:2048",
                    "-keyout", key_path,
                    "-out", req_path,
                    "-subj", make_subject("web-platform-tests"),
                    "-passout", "pass:%s" % self.password)

            openssl("ca",
                    "-batch",
                    "-create_serial",
                    "-days", "1",
                    "-keyfile", key_path,
                    "-passin", "pass:%s" % self.password,
                    "-selfsign",
                    "-extensions", "v3_ca",
                    "-in", req_path,
                    "-out", cert_path)

        os.unlink(req_path)

        self._ca_key_path, self._ca_cert_path = key_path, cert_path

    def host_cert_path(self, hosts):
        """Get a tuple of (private key path, certificate path) for a host,
        generating new ones if necessary.

        hosts must be a list of all hosts to appear on the certificate, with
        the primary hostname first."""
        hosts = tuple(hosts)
        if hosts not in self.host_certificates:
            if not self.force_regenerate:
                key_cert = self._load_host_cert(hosts)
            else:
                key_cert = None
            if key_cert is None:
                key, cert = self._generate_host_cert(hosts)
            else:
                key, cert = key_cert
            self.host_certificates[hosts] = key, cert

        return self.host_certificates[hosts]

    def _load_host_cert(self, hosts):
        host = hosts[0]
        key_path = self.path("%s.key" % host)
        cert_path = self.path("%s.pem" % host)

        # TODO: check that this cert was signed by the CA cert
        if self.check_key_cert(key_path, cert_path, hosts):
            self.logger.info("Using existing host cert")
            return key_path, cert_path

    def _generate_host_cert(self, hosts):
        host = hosts[0]
        if self._ca_key_path is None:
            self._generate_ca()
        ca_key_path = self._ca_key_path

        assert os.path.exists(ca_key_path)

        path = self.path

        req_path = path("wpt.req")
        cert_path = path("%s.pem" % host)
        key_path = path("%s.key" % host)

        self.logger.info("Generating new host cert")

        with self._config_openssl(hosts) as openssl:
            openssl("req",
                    "-batch",
                    "-newkey", "rsa:2048",
                    "-keyout", key_path,
                    "-in", ca_key_path,
                    "-nodes",
                    "-out", req_path)

            openssl("ca",
                    "-batch",
                    "-in", req_path,
                    "-passin", "pass:%s" % self.password,
                    "-subj", make_subject(host),
                    "-out", cert_path)

        os.unlink(req_path)

        return key_path, cert_path