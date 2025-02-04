import logging
import sys
import json

from hashlib import sha1
from saml2.httpbase import HTTPBase
from saml2.extension.idpdisc import BINDING_DISCO
from saml2.extension.idpdisc import DiscoveryResponse
from saml2.md import EntitiesDescriptor

from saml2.mdie import to_dict

from saml2 import md
from saml2 import samlp
from saml2 import SAMLError
from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_SOAP
from saml2.s_utils import UnsupportedBinding, UnknownPrincipal
from saml2.sigver import split_len
from saml2.validate import valid_instance
from saml2.time_util import valid
from saml2.validate import NotValid
from saml2.sigver import security_context
from importlib import import_module

__author__ = 'rolandh'

logger = logging.getLogger(__name__)


class ToOld(Exception):
    pass


REQ2SRV = {
    # IDP
    "authn_request": "single_sign_on_service",
    "name_id_mapping_request": "name_id_mapping_service",
    # AuthnAuthority
    "authn_query": "authn_query_service",
    # AttributeAuthority
    "attribute_query": "attribute_service",
    # PDP
    "authz_decision_query": "authz_service",
    # AuthnAuthority + IDP + PDP + AttributeAuthority
    "assertion_id_request": "assertion_id_request_service",
    # IDP + SP
    "logout_request": "single_logout_service",
    "manage_name_id_request": "manage_name_id_service",
    "artifact_query": "artifact_resolution_service",
    # SP
    "assertion_response": "assertion_consumer_service",
    "attribute_response": "attribute_consuming_service",
    "discovery_service_request": "discovery_response"
}


ENTITYATTRIBUTES = "urn:oasis:names:tc:SAML:metadata:attribute&EntityAttributes"
ENTITY_CATEGORY = "http://macedir.org/entity-category"
ENTITY_CATEGORY_SUPPORT = "http://macedir.org/entity-category-support"

# ---------------------------------------------------


def destinations(srvs):
    return [s["location"] for s in srvs]


def attribute_requirement(entity):
    res = {"required": [], "optional": []}
    for acs in entity["attribute_consuming_service"]:
        for attr in acs["requested_attribute"]:
            if "is_required" in attr and attr["is_required"] == "true":
                res["required"].append(attr)
            else:
                res["optional"].append(attr)
    return res


def name(ent, langpref="en"):
    try:
        org = ent["organization"]
    except KeyError:
        return None

    for info in ["organization_display_name",
                 "organization_name",
                 "organization_url"]:
        try:
            for item in org[info]:
                if item["lang"] == langpref:
                    return item["text"]
        except KeyError:
            pass
    return None


def repack_cert(cert):
    part = cert.split("\n")
    if len(part) == 1:
        part = part[0].strip()
        return "\n".join(split_len(part, 64))
    else:
        return "\n".join([s.strip() for s in part])


class MetaData(object):
    def __init__(self, onts, attrc, metadata="", node_name=None,
                 check_validity=True, security=None, **kwargs):
        self.onts = onts
        self.attrc = attrc
        self.entity = {}
        self.metadata = metadata
        self.security = security
        self.node_name = node_name
        self.entities_descr = None
        self.entity_descr = None
        self.check_validity = check_validity
        
    def items(self):
        return self.entity.items()

    def keys(self):
        return self.entity.keys()

    def values(self):
        return self.entity.values()

    def __contains__(self, item):
        return item in self.entity

    def __getitem__(self, item):
        return self.entity[item]

    def do_entity_descriptor(self, entity_descr):
        if self.check_validity:
            try:
                if not valid(entity_descr.valid_until):
                    logger.info("Entity descriptor (entity id:%s) to old" % (
                        entity_descr.entity_id,))
                    return
            except AttributeError:
                pass

        # have I seen this entity_id before ? If so if log: ignore it
        if entity_descr.entity_id in self.entity:
            print >> sys.stderr, \
                "Duplicated Entity descriptor (entity id: '%s')" % \
                entity_descr.entity_id
            return

        _ent = to_dict(entity_descr, self.onts)
        flag = 0
        # verify support for SAML2
        for descr in ["spsso", "idpsso", "role", "authn_authority",
                      "attribute_authority", "pdp", "affiliation"]:
            _res = []
            try:
                _items = _ent["%s_descriptor" % descr]
            except KeyError:
                continue

            if descr == "affiliation":  # Not protocol specific
                flag += 1
                continue

            for item in _items:
                for prot in item["protocol_support_enumeration"].split(" "):
                    if prot == samlp.NAMESPACE:
                        item["protocol_support_enumeration"] = prot
                        _res.append(item)
                        break
            if not _res:
                del _ent["%s_descriptor" % descr]
            else:
                flag += 1

        if flag:
            self.entity[entity_descr.entity_id] = _ent

    def parse(self, xmlstr):
        self.entities_descr = md.entities_descriptor_from_string(xmlstr)

        if not self.entities_descr:
            self.entity_descr = md.entity_descriptor_from_string(xmlstr)
            if self.entity_descr:
                self.do_entity_descriptor(self.entity_descr)
        else:
            try:
                valid_instance(self.entities_descr)
            except NotValid, exc:
                logger.error(exc.args[0])
                return

            if self.check_validity:
                try:
                    if not valid(self.entities_descr.valid_until):
                        raise ToOld(
                            "Metadata not valid anymore, it's after %s" % (
                                self.entities_descr.valid_until,))
                except AttributeError:
                    pass

            for entity_descr in self.entities_descr.entity_descriptor:
                self.do_entity_descriptor(entity_descr)

    def load(self):
        self.parse(self.metadata)

    def service(self, entity_id, typ, service, binding=None):
        """ Get me all services with a specified
        entity ID and type, that supports the specified version of binding.

        :param entity_id: The EntityId
        :param typ: Type of service (idp, attribute_authority, ...)
        :param service: which service that is sought for
        :param binding: A binding identifier
        :return: list of service descriptions.
            Or if no binding was specified a list of 2-tuples (binding, srv)
        """

        logger.debug("service(%s, %s, %s, %s)" % (entity_id, typ, service,
                                                   binding))
        try:
            srvs = []
            for t in self[entity_id][typ]:
                try:
                    srvs.extend(t[service])
                except KeyError:
                    pass
        except KeyError:
            return None

        if not srvs:
            return srvs

        if binding:
            res = []
            for srv in srvs:
                if srv["binding"] == binding:
                    res.append(srv)
        else:
            res = {}
            for srv in srvs:
                try:
                    res[srv["binding"]].append(srv)
                except KeyError:
                    res[srv["binding"]] = [srv]
        logger.debug("service => %s" % res)
        return res

    def ext_service(self, entity_id, typ, service, binding):
        try:
            srvs = self[entity_id][typ]
        except KeyError:
            return None

        if not srvs:
            return srvs

        res = []
        for srv in srvs:
            if "extensions" in srv:
                for elem in srv["extensions"]["extension_elements"]:
                    if elem["__class__"] == service:
                        if elem["binding"] == binding:
                            res.append(elem)

        return res

    def any(self, typ, service, binding=None):
        """
        Return any entity that matches the specification

        :param typ:
        :param service:
        :param binding:
        :return:
        """
        res = {}
        for ent in self.keys():
            bind = self.service(ent, typ, service, binding)
            if bind:
                res[ent] = bind

        return res

    def bindings(self, entity_id, typ, service):
        """
        Get me all the bindings that are registered for a service entity

        :param entity_id:
        :param service:
        :return:
        """

        return self.service(entity_id, typ, service)

    def attribute_requirement(self, entity_id, index=0):
        """ Returns what attributes the SP requires and which are optional
        if any such demands are registered in the Metadata.

        :param entity_id: The entity id of the SP
        :param index: which of the attribute consumer services its all about
        :return: 2-tuple, list of required and list of optional attributes
        """

        res = {"required": [], "optional": []}

        try:
            for sp in self[entity_id]["spsso_descriptor"]:
                _res = attribute_requirement(sp)
                res["required"].extend(_res["required"])
                res["optional"].extend(_res["optional"])
        except KeyError:
            return None

        return res

    def dumps(self):
        return json.dumps(self.items(), indent=2)

    def with_descriptor(self, descriptor):
        res = {}
        desc = "%s_descriptor" % descriptor
        for eid, ent in self.items():
            if desc in ent:
                res[eid] = ent
        return res

    def __str__(self):
        return "%s" % self.items()

    def construct_source_id(self):
        res = {}
        for eid, ent in self.items():
            for desc in ["spsso_descriptor", "idpsso_descriptor"]:
                try:
                    for srv in ent[desc]:
                        if "artifact_resolution_service" in srv:
                            s = sha1(eid)
                            res[s.digest()] = ent
                except KeyError:
                    pass

        return res

    def entity_categories(self, entity_id):
        res = []
        if "extensions" in self[entity_id]:
            for elem in self[entity_id]["extensions"]["extension_elements"]:
                if elem["__class__"] == ENTITYATTRIBUTES:
                    for attr in elem["attribute"]:
                        res.append(attr["text"])

        return res

    def __eq__(self, other):
        try:
            assert isinstance(other, MetaData)
        except AssertionError:
            return False

        if len(self.entity) != len(other.entity):
            return False

        if set(self.entity.keys()) != set(other.entity.keys()):
            return False

        for key, item in self.entity.items():
            try:
                assert item == other[key]
            except AssertionError:
                return False

        return True


class MetaDataFile(MetaData):
    """
    Handles Metadata file on the same machine. The format of the file is
    the SAML Metadata format.
    """
    def __init__(self, onts, attrc, filename, cert=None, **kwargs):
        MetaData.__init__(self, onts, attrc, **kwargs)
        self.filename = filename
        self.cert = cert

    def get_metadata_content(self):
        return open(self.filename).read()

    def load(self):
        _txt = self.get_metadata_content()
        if self.cert:
            node_name = self.node_name \
                or "%s:%s" % (md.EntitiesDescriptor.c_namespace,
                              md.EntitiesDescriptor.c_tag)

            if self.security.verify_signature(_txt,
                                              node_name=node_name,
                                              cert_file=self.cert):
                self.parse(_txt)
                return True
        else:
            self.parse(_txt)
            return True


class MetaDataLoader(MetaDataFile):
    """
    Handles Metadata file loaded by a passed in function.
    The format of the file is the SAML Metadata format.
    """
    def __init__(self, onts, attrc, loader_callable, cert=None,
                 security=None, **kwargs):
        MetaData.__init__(self, onts, attrc, **kwargs)
        self.metadata_provider_callable = self.get_metadata_loader(
            loader_callable)
        self.cert = cert
        self.security = security

    @staticmethod
    def get_metadata_loader(func):
        if callable(func):
            return func

        i = func.rfind('.')
        module, attr = func[:i], func[i + 1:]
        try:
            mod = import_module(module)
        except Exception, e:
            raise RuntimeError(
                'Cannot find metadata provider function %s: "%s"' % (func, e))

        try:
            metadata_loader = getattr(mod, attr)
        except AttributeError:
            raise RuntimeError(
                'Module "%s" does not define a "%s" metadata loader' % (
                    module, attr))

        if not callable(metadata_loader):
            raise RuntimeError(
                'Metadata loader %s.%s must be callable' % (module, attr))

        return metadata_loader

    def get_metadata_content(self):
        return self.metadata_provider_callable()


class MetaDataExtern(MetaData):
    """
    Class that handles metadata store somewhere on the net.
    Accessible but HTTP GET.
    """

    def __init__(self, onts, attrc, url, security, cert, http, **kwargs):
        """
        :params onts:
        :params attrc:
        :params url:
        :params security: SecurityContext()
        :params cert:
        :params http:
        """
        MetaData.__init__(self, onts, attrc, **kwargs)
        self.url = url
        self.security = security
        self.cert = cert
        self.http = http

    def load(self):
        """ Imports metadata by the use of HTTP GET.
        If the fingerprint is known the file will be checked for
        compliance before it is imported.
        """
        response = self.http.send(self.url)
        if response.status_code == 200:
            node_name = self.node_name \
                or "%s:%s" % (md.EntitiesDescriptor.c_namespace,
                              md.EntitiesDescriptor.c_tag)

            _txt = response.text.encode("utf-8")
            if self.cert:
                if self.security.verify_signature(_txt,
                                                  node_name=node_name,
                                                  cert_file=self.cert):
                    self.parse(_txt)
                    return True
            else:
                self.parse(_txt)
                return True
        else:
            logger.info("Response status: %s" % response.status_code)
        return False


class MetaDataMD(MetaData):
    """
    Handles locally stored metadata, the file format is the text representation
    of the Python representation of the metadata.
    """
    def __init__(self, onts, attrc, filename, **kwargs):
        MetaData.__init__(self, onts, attrc, **kwargs)
        self.filename = filename

    def load(self):
        for key, item in json.loads(open(self.filename).read()):
            self.entity[key] = item


class MetadataStore(object):
    def __init__(self, onts, attrc, config, ca_certs=None,
                 disable_ssl_certificate_validation=False):
        """
        :params onts:
        :params attrc:
        :params config: Config()
        :params ca_certs:
        :params disable_ssl_certificate_validation:
        """
        self.onts = onts
        self.attrc = attrc
        self.http = HTTPBase(verify=disable_ssl_certificate_validation,
                             ca_bundle=ca_certs)
        self.security = security_context(config)
        self.ii = 0
        self.metadata = {}

    def load(self, typ, *args, **kwargs):
        if typ == "local":
            key = args[0]
            _md = MetaDataFile(self.onts, self.attrc, args[0])
        elif typ == "inline":
            self.ii += 1
            key = self.ii
            _md = MetaData(self.onts, self.attrc, args[0], **kwargs)
        elif typ == "remote":
            key = kwargs["url"]
            _md = MetaDataExtern(self.onts, self.attrc,
                                 kwargs["url"], self.security,
                                 kwargs["cert"], self.http,
                                 node_name=kwargs.get('node_name'))
        elif typ == "mdfile":
            key = args[0]
            _md = MetaDataMD(self.onts, self.attrc, args[0])
        elif typ == "loader":
            key = args[0]
            _md = MetaDataLoader(self.onts, self.attrc, args[0])
        else:
            raise SAMLError("Unknown metadata type '%s'" % typ)

        _md.load()
        self.metadata[key] = _md

    def imp(self, spec):
        for key, vals in spec.items():
            for val in vals:
                if isinstance(val, dict):
                    self.load(key, **val)
                else:
                    self.load(key, val)

    def service(self, entity_id, typ, service, binding=None):
        known_principal = False
        for key, _md in self.metadata.items():
            srvs = _md.service(entity_id, typ, service, binding)
            if srvs:
                return srvs
            elif srvs is None:
                pass
            else:
                known_principal = True

        if known_principal:
            logger.error("Unsupported binding: %s (%s)" % (binding, entity_id))
            raise UnsupportedBinding(binding)
        else:
            logger.error("Unknown principal: %s" % entity_id)
            raise UnknownPrincipal(entity_id)

    def ext_service(self, entity_id, typ, service, binding=None):
        known_principal = False
        for key, _md in self.metadata.items():
            srvs = _md.ext_service(entity_id, typ, service, binding)
            if srvs:
                return srvs
            elif srvs is None:
                pass
            else:
                known_principal = True

        if known_principal:
            raise UnsupportedBinding(binding)
        else:
            raise UnknownPrincipal(entity_id)

    def single_sign_on_service(self, entity_id, binding=None, typ="idpsso"):
        # IDP

        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "idpsso_descriptor",
                            "single_sign_on_service", binding)

    def name_id_mapping_service(self, entity_id, binding=None, typ="idpsso"):
        # IDP
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "idpsso_descriptor",
                            "name_id_mapping_service", binding)

    def authn_query_service(self, entity_id, binding=None,
                            typ="authn_authority"):
        # AuthnAuthority
        if binding is None:
            binding = BINDING_SOAP
        return self.service(entity_id, "authn_authority_descriptor",
                            "authn_query_service", binding)

    def attribute_service(self, entity_id, binding=None,
                          typ="attribute_authority"):
        # AttributeAuthority
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "attribute_authority_descriptor",
                            "attribute_service", binding)

    def authz_service(self, entity_id, binding=None, typ="pdp"):
        # PDP
        if binding is None:
            binding = BINDING_SOAP
        return self.service(entity_id, "pdp_descriptor",
                             "authz_service", binding)

    def assertion_id_request_service(self, entity_id, binding=None, typ=None):
        # AuthnAuthority + IDP + PDP + AttributeAuthority
        if typ is None:
            raise AttributeError("Missing type specification")
        if binding is None:
            binding = BINDING_SOAP
        return self.service(entity_id, "%s_descriptor" % typ,
                             "assertion_id_request_service", binding)

    def single_logout_service(self, entity_id, binding=None, typ=None):
        # IDP + SP
        if typ is None:
            raise AttributeError("Missing type specification")
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "%s_descriptor" % typ,
                             "single_logout_service", binding)

    def manage_name_id_service(self, entity_id, binding=None, typ=None):
        # IDP + SP
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "%s_descriptor" % typ,
                             "manage_name_id_service", binding)

    def artifact_resolution_service(self, entity_id, binding=None, typ=None):
        # IDP + SP
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "%s_descriptor" % typ,
                             "artifact_resolution_service", binding)

    def assertion_consumer_service(self, entity_id, binding=None, _="spsso"):
        # SP
        if binding is None:
            binding = BINDING_HTTP_POST
        return self.service(entity_id, "spsso_descriptor",
                             "assertion_consumer_service", binding)

    def attribute_consuming_service(self, entity_id, binding=None, _="spsso"):
        # SP
        if binding is None:
            binding = BINDING_HTTP_REDIRECT
        return self.service(entity_id, "spsso_descriptor",
                             "attribute_consuming_service", binding)

    def discovery_response(self, entity_id, binding=None, _="spsso"):
        if binding is None:
            binding = BINDING_DISCO
        return self.ext_service(entity_id, "spsso_descriptor",
                                "%s&%s" % (DiscoveryResponse.c_namespace,
                                           DiscoveryResponse.c_tag),
                                binding)

    def attribute_requirement(self, entity_id, index=0):
        for _md in self.metadata.values():
            if entity_id in _md:
                return _md.attribute_requirement(entity_id, index)

    def keys(self):
        res = []
        for _md in self.metadata.values():
            res.extend(_md.keys())
        return res

    def __getitem__(self, item):
        for _md in self.metadata.values():
            try:
                return _md[item]
            except KeyError:
                pass

        raise KeyError(item)

    def __setitem__(self, key, value):
        self.metadata[key] = value

    def entities(self):
        num = 0
        for _md in self.metadata.values():
            num += len(_md.items())

        return num

    def __len__(self):
        return len(self.metadata)

    def with_descriptor(self, descriptor):
        res = {}
        for _md in self.metadata.values():
            res.update(_md.with_descriptor(descriptor))
        return res

    def name(self, entity_id, langpref="en"):
        for _md in self.metadata.values():
            if entity_id in _md.items():
                return name(_md[entity_id], langpref)
        return None

    def certs(self, entity_id, descriptor, use="signing"):
        ent = self.__getitem__(entity_id)
        if descriptor == "any":
            res = []
            for descr in ["spsso", "idpsso", "role", "authn_authority",
                          "attribute_authority", "pdp"]:
                try:
                    srvs = ent["%s_descriptor" % descr]
                except KeyError:
                    continue

                for srv in srvs:
                    for key in srv["key_descriptor"]:
                        if "use" in key and key["use"] == use:
                            for dat in key["key_info"]["x509_data"]:
                                cert = repack_cert(
                                    dat["x509_certificate"]["text"])
                                if cert not in res:
                                    res.append(cert)
                        elif not "use" in key:
                            for dat in key["key_info"]["x509_data"]:
                                cert = repack_cert(
                                    dat["x509_certificate"]["text"])
                                if cert not in res:
                                    res.append(cert)
        else:
            srvs = ent["%s_descriptor" % descriptor]

            res = []
            for srv in srvs:
                for key in srv["key_descriptor"]:
                    if "use" in key and key["use"] == use:
                        for dat in key["key_info"]["x509_data"]:
                            res.append(dat["x509_certificate"]["text"])
                    elif not "use" in key:
                        for dat in key["key_info"]["x509_data"]:
                            res.append(dat["x509_certificate"]["text"])
        return res

    def vo_members(self, entity_id):
        ad = self.__getitem__(entity_id)["affiliation_descriptor"]
        return [m["text"] for m in ad["affiliate_member"]]

    def entity_categories(self, entity_id):
        """
        Get a list of entity categories for an entity id.

        :param entity_id: Entity id
        :return: Entity categories

        :type entity_id: string
        :rtype: [string]
        """
        attributes = self.entity_attributes(entity_id)
        return attributes.get(ENTITY_CATEGORY, [])

    def supported_entity_categories(self, entity_id):
        """
        Get a list of entity category support for an entity id.

        :param entity_id: Entity id
        :return: Entity category support

        :type entity_id: string
        :rtype: [string]
        """
        attributes = self.entity_attributes(entity_id)
        return attributes.get(ENTITY_CATEGORY_SUPPORT, [])

    def entity_attributes(self, entity_id):
        """
        Get all entity attributes for an entry in the metadata.

        Example return data:

        {'http://macedir.org/entity-category': ['something', 'something2'],
         'http://example.org/saml-foo': ['bar']}

        :param entity_id: Entity id
        :return: dict with keys and value-lists from metadata

        :type entity_id: string
        :rtype: dict
        """
        res = {}
        try:
            ext = self.__getitem__(entity_id)["extensions"]
        except KeyError:
            return res
        for elem in ext["extension_elements"]:
            if elem["__class__"] == ENTITYATTRIBUTES:
                for attr in elem["attribute"]:
                    if attr["name"] not in res:
                        res[attr["name"]] = []
                    res[attr["name"]] += [v["text"] for v in attr[
                        "attribute_value"]]
        return res

    def bindings(self, entity_id, typ, service):
        for _md in self.metadata.values():
            if entity_id in _md.items():
                return _md.bindings(entity_id, typ, service)

        return None

    def __str__(self):
        _str = ["{"]
        for key, val in self.metadata.items():
            _str.append("%s: %s" % (key, val))
        _str.append("}")
        return "\n".join(_str)

    def construct_source_id(self):
        res = {}
        for _md in self.metadata.values():
            res.update(_md.construct_source_id())
        return res

    def items(self):
        res = {}
        for _md in self.metadata.values():
            res.update(_md.items())
        return res.items()

    def _providers(self, descriptor):
        res = []
        for _md in self.metadata.values():
            for ent_id, ent_desc in _md.items():
                if descriptor in ent_desc:
                    res.append(ent_id)
        return res

    def service_providers(self):
        return self._providers("spsso_descriptor")

    def identity_providers(self):
        return self._providers("idpsso_descriptor")

    def attribute_authorities(self):
        return self._providers("attribute_authority")

    def dumps(self, format="local"):
        """
        Dumps the content in standard metadata format or the pysaml2 metadata
        format

        :param format: Which format to dump in
        :return: a string
        """
        if format == "local":
            res = EntitiesDescriptor()
            for _md in self.metadata.values():
                try:
                    res.entity_descriptor.extend(_md.entities_descr.entity_descriptor)
                except AttributeError:
                    res.entity_descriptor.append(_md.entity_descr)

            return "%s" % res
        elif format == "md":
            return json.dumps(self.items(), indent=2)

