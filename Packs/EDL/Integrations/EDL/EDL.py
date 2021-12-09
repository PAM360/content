import tempfile

import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

import re

from base64 import b64decode
from flask import Flask, Response, request
from netaddr import IPSet
from typing import Any, Dict, cast, Iterable, Callable, IO
from math import ceil
import urllib3
import dateparser
import hashlib
import json

# Disable insecure warnings
urllib3.disable_warnings()

''' GLOBAL VARIABLES '''
INTEGRATION_NAME: str = 'EDL'
PAGE_SIZE: int = 2000
PAN_OS_MAX_URL_LEN = 255
APP: Flask = Flask('demisto-edl')
EDL_LIMIT_ERR_MSG: str = 'Please provide a valid integer for EDL Size'
EDL_OFFSET_ERR_MSG: str = 'Please provide a valid integer for Starting Index'
EDL_COLLAPSE_ERR_MSG: str = 'The Collapse parameter can only get the following: 0 - Dont Collapse, ' \
                            '1 - Collapse to Ranges, 2 - Collapse to CIDRS'
EDL_MISSING_REFRESH_ERR_MSG: str = 'Refresh Rate must be "number date_range_unit", examples: (2 hours, 4 minutes, ' \
                                   '6 months, 1 day, etc.)'
EDL_FORMAT_ERR_MSG: str = 'Please provide a valid format from: text, json, json-seq, xsoar-json, csv, mgw and proxysg'
EDL_MWG_TYPE_ERR_MSG: str = 'The McAFee Web Gateway type can only be one of the following: string,' \
                            ' applcontrol, dimension, category, ip, mediatype, number, regex'
EDL_NO_URLS_IN_PROXYSG_FORMAT = 'ProxySG format only outputs URLs - no URLs found in the current query'

EDL_ON_DEMAND_KEY: str = 'UpdateEDL'
EDL_ON_DEMAND_CACHE_PATH: str = ''

''' REFORMATTING REGEXES '''
_PROTOCOL_REMOVAL = re.compile('^(?:[a-z]+:)*//')
_PORT_REMOVAL = re.compile(r'^((?:[a-z]+:)*//([a-z0-9\-\.]+)|([a-z0-9\-\.]+))(?:\:[0-9]+)*')
_URL_WITHOUT_PORT = r'\g<1>'
_INVALID_TOKEN_REMOVAL = re.compile(r'(?:[^\./+=\?&]+\*[^\./+=\?&]*)|(?:[^\./+=\?&]*\*[^\./+=\?&]+)')
_BROAD_PATTERN = re.compile(r'^(?:\*\.)+[a-zA-Z]+(?::[0-9]+)?$')

DONT_COLLAPSE = "Don't Collapse"
COLLAPSE_TO_CIDR = "To CIDRS"
COLLAPSE_TO_RANGES = "To Ranges"

MIMETYPE_JSON_SEQ: str = 'application/json-seq'
MIMETYPE_JSON: str = 'application/json'
MIMETYPE_CSV: str = 'text/csv'
MIMETYPE_TEXT: str = 'text/plain'

FORMAT_CSV: str = 'csv'
FORMAT_TEXT: str = 'text'
FORMAT_JSON_SEQ: str = 'json-seq'
FORMAT_JSON: str = 'json'
FORMAT_ARG_MWG = 'mwg'
FORMAT_ARG_BLUECOAT = 'bluecoat'
FORMAT_ARG_PROXYSG = 'proxysg'
FORMAT_MWG: str = 'McAfee Web Gateway'
FORMAT_PROXYSG: str = "Symantec ProxySG"
FORMAT_XSOAR_JSON: str = 'XSOAR json'
FORMAT_ARG_XSOAR_JSON: str = 'xsoar-json'
FORMAT_XSOAR_JSON_SEQ: str = 'XSOAR json-seq'
FORAMT_ARG_XSOAR_JSON_SEQ: str = 'xsoar-seq'

MWG_TYPE_OPTIONS = ["string", "applcontrol", "dimension", "category", "ip", "mediatype", "number", "regex"]

'''Request Arguments Class'''


class RequestArguments:
    CTX_QUERY_KEY = 'last_query'
    CTX_OUT_FORMAT = 'out_format'
    CTX_LIMIT_KEY = 'last_limit'
    CTX_OFFSET_KEY = 'last_offset'
    CTX_INVALIDS_KEY = 'drop_invalids'
    CTX_PORT_STRIP_KEY = 'url_port_stripping'
    CTX_COLLAPSE_IPS_KEY = 'collapse_ips'
    CTX_EMPTY_EDL_COMMENT_KEY = 'add_comment_if_empty'
    CTX_MWG_TYPE = 'mwg_type'
    CTX_CATEGORY_DEFAULT = 'bc_category'
    CTX_CATEGORY_ATTRIBUTE = 'category_attribute'
    CTX_FIELDS_TO_PRESENT = 'fields_to_present'
    CTX_CSV_TEXT = 'csv_text'
    CTX_SORT_FIELDS = 'sort_field'
    CTX_SORT_ORDER = 'sort_order'
    CTX_PROTOCOL_STRIP_KEY = 'url_protocol_stripping'

    FILTER_FIELDS_ON_FORMAT_TEXT = "name,type"
    FILTER_FIELDS_ON_FORMAT_MWG = "name,type,sourceBrands"
    FILTER_FIELDS_ON_FORMAT_PROXYSG = "name,type,proxysgcategory"
    FILTER_FIELDS_ON_FORMAT_CSV = "name,type,createdTime,firstSeen"
    FILTER_FIELDS_ON_FORMAT_JSON = "name,type,createdTime,firstSeen"
    FILTER_FIELDS_ON_FORMAT_XSOAR_JSON = "name,type,createdTime,firstSeen"

    def __init__(self,
                 query: str,
                 out_format: str = FORMAT_TEXT,
                 limit: int = 10000,
                 offset: int = 0,
                 url_port_stripping: bool = False,
                 drop_invalids: bool = False,
                 collapse_ips: str = DONT_COLLAPSE,
                 add_comment_if_empty: bool = True,
                 mwg_type: str = 'string',
                 category_default: str = 'bc_category',
                 category_attribute: str = '',
                 fields_to_present: str = None,
                 csv_text: bool = False,
                 sort_field: str = '',
                 sort_order: str = '',
                 url_protocol_stripping: bool = False,
                 ):

        self.query = query
        self.out_format = out_format
        self.limit = try_parse_integer(limit, EDL_LIMIT_ERR_MSG)
        self.offset = try_parse_integer(offset, EDL_OFFSET_ERR_MSG)
        self.url_port_stripping = url_port_stripping
        self.url_protocol_stripping = url_protocol_stripping
        self.drop_invalids = drop_invalids
        self.collapse_ips = collapse_ips
        self.add_comment_if_empty = add_comment_if_empty
        self.mwg_type = mwg_type
        self.category_default = category_default
        self.category_attribute = []  # type:List
        self.fields_to_present = self.get_fields_to_present(fields_to_present)
        self.csv_text = csv_text
        self.sort_field = sort_field
        self.sort_order = sort_order

        if category_attribute is not None:
            category_attribute_list = category_attribute.split(',')

            if len(category_attribute_list) != 1 or '' not in category_attribute_list:
                self.category_attribute = category_attribute_list

    def to_context_json(self):
        return {
            self.CTX_QUERY_KEY: self.query,
            self.CTX_OUT_FORMAT: self.out_format,
            self.CTX_LIMIT_KEY: self.limit,
            self.CTX_OFFSET_KEY: self.offset,
            self.CTX_INVALIDS_KEY: self.drop_invalids,
            self.CTX_PORT_STRIP_KEY: self.url_port_stripping,
            self.CTX_COLLAPSE_IPS_KEY: self.collapse_ips,
            self.CTX_EMPTY_EDL_COMMENT_KEY: self.add_comment_if_empty,
            self.CTX_MWG_TYPE: self.mwg_type,
            self.CTX_CATEGORY_DEFAULT: self.category_default,
            self.CTX_CATEGORY_ATTRIBUTE: self.category_attribute,
            self.CTX_FIELDS_TO_PRESENT: self.fields_to_present,
            self.CTX_CSV_TEXT: self.csv_text,
            self.CTX_SORT_FIELDS: self.sort_field,
            self.CTX_SORT_ORDER: self.sort_order,
            self.CTX_PROTOCOL_STRIP_KEY: self.url_protocol_stripping
        }

    @classmethod
    def from_context_json(cls, ctx_dict):
        """Returns an initiated instance of the class from a json"""
        return cls(
            **assign_params(
                query=ctx_dict.get(cls.CTX_QUERY_KEY),
                out_format=ctx_dict.get(cls.CTX_OUT_FORMAT),
                limit=ctx_dict.get(cls.CTX_LIMIT_KEY),
                offset=ctx_dict.get(cls.CTX_OFFSET_KEY),
                drop_invalids=ctx_dict.get(cls.CTX_INVALIDS_KEY),
                url_port_stripping=ctx_dict.get(cls.CTX_PORT_STRIP_KEY),
                collapse_ips=ctx_dict.get(cls.CTX_COLLAPSE_IPS_KEY),
                add_comment_if_empty=ctx_dict.get(cls.CTX_EMPTY_EDL_COMMENT_KEY),
                mwg_type=ctx_dict.get(cls.CTX_MWG_TYPE),
                category_default=ctx_dict.get(cls.CTX_CATEGORY_DEFAULT),
                category_attributeself=ctx_dict.get(cls.CTX_CATEGORY_ATTRIBUTE),
                fields_to_present=ctx_dict.get(cls.CTX_FIELDS_TO_PRESENT),
                csv_text=ctx_dict.get(cls.CTX_CSV_TEXT),
                sort_field=ctx_dict.get(cls.CTX_SORT_FIELDS),
                sort_order=ctx_dict.get(cls.CTX_SORT_ORDER),
                url_protocol_stripping=ctx_dict.get(cls.CTX_PROTOCOL_STRIP_KEY),
            )
        )

    def get_fields_to_present(self, fields_to_present: str):
        # based on func ToIoC https://github.com/demisto/server/blob/master/domain/insight.go

        if fields_to_present == 'use_legacy_query':
            return None

        fields_for_format = {
            FORMAT_TEXT: self.FILTER_FIELDS_ON_FORMAT_TEXT,
            FORMAT_CSV: self.FILTER_FIELDS_ON_FORMAT_CSV,
            FORMAT_JSON: self.FILTER_FIELDS_ON_FORMAT_JSON,
            FORMAT_XSOAR_JSON: self.FILTER_FIELDS_ON_FORMAT_XSOAR_JSON,
            FORMAT_JSON_SEQ: self.FILTER_FIELDS_ON_FORMAT_JSON,
            FORMAT_XSOAR_JSON_SEQ: self.FILTER_FIELDS_ON_FORMAT_XSOAR_JSON,
            FORMAT_MWG: self.FILTER_FIELDS_ON_FORMAT_MWG,
            FORMAT_PROXYSG: self.FILTER_FIELDS_ON_FORMAT_PROXYSG
        }
        if self.out_format in [FORMAT_CSV, FORMAT_JSON, FORMAT_XSOAR_JSON, FORMAT_JSON_SEQ, FORMAT_XSOAR_JSON_SEQ] and\
                fields_to_present:
            if 'all' in argToList(fields_to_present):
                return None
            else:
                return fields_to_present

        return fields_for_format.get(self.out_format, self.FILTER_FIELDS_ON_FORMAT_TEXT)


''' HELPER FUNCTIONS '''


def iterable_to_str(iterable: Iterable, delimiter: str = '\n') -> str:
    """
    Transforms an iterable object to an str, with a custom delimiter between each item
    """
    str_res = ""
    if iterable:
        try:
            iter(iterable)
        except TypeError:
            raise DemistoException(f'non iterable object provided to iterable_to_str: {iterable}')
        str_res = delimiter.join(map(str, iterable))
    return str_res


def create_new_edl(request_args: RequestArguments) -> str:
    """
    Gets indicators from XSOAR server using IndicatorsSearcher and formats them

    Parameters:
        request_args: Request arguments

    Returns: Formatted indicators to display in EDL
    """
    limit = request_args.offset + request_args.limit
    indicator_searcher = IndicatorsSearcher(
        filter_fields=request_args.fields_to_present,
        query=request_args.query,
        size=PAGE_SIZE,
        limit=limit
    )
    if request_args.out_format == 'text':
        current_limit = int(limit*1)
        while True:
            indicator_searcher.limit = current_limit
            new_iocs = find_indicators_to_limit(indicator_searcher, request_args)
            new_iocs = text_format(new_iocs, request_args)
            edl_size = 0
            new_iocs.seek(0)
            for count, line in enumerate(new_iocs):
                edl_size = count
            # continue searching iocs if 1) iocs was truncated or 2) got all available iocs
            if edl_size + 1 >= current_limit or indicator_searcher.total <= current_limit:
                break
            else:
                current_limit = int(current_limit*1.1)
        new_iocs.seek(0)
        return new_iocs.read() + '\n' + str(edl_size)
    else:
        new_iocs = find_indicators_to_limit(indicator_searcher, request_args)
        new_iocs.seek(0)
        return new_iocs.read()


def replace_field_name_to_output_format(fields: str):
    fields_list = argToList(fields)
    new_list = []
    for field in fields_list:
        if field == 'name':
            field = 'value'
        elif field == 'type':
            field = 'indicator_type'
        new_list.append(field)
    return new_list


def find_indicators_to_limit(indicator_searcher: IndicatorsSearcher, request_args: RequestArguments) -> Union[
    IO, IO[str]]:
    """
    Finds indicators using while loop with demisto.searchIndicators, and returns result and last page

    Parameters:
        indicator_searcher (IndicatorsSearcher): The indicator searcher used to look for indicators
        request_args (RequestArguments)
    Returns:
        (list): List of Indicators dict with value,indicator_type keys
    """
    f = tempfile.TemporaryFile(mode='w+t')
    list_fields = replace_field_name_to_output_format(request_args.fields_to_present)
    headers_was_writen = False
    files_by_category = {}
    try:
        for ioc_res in indicator_searcher:
            fetched_iocs = ioc_res.get('iocs') or []
            for ioc in fetched_iocs:
                if request_args.out_format == FORMAT_PROXYSG:
                    files_by_category, return_indicator = create_proxysg_out_format(ioc, files_by_category,
                                                                                    request_args.category_attribute,
                                                                                    request_args.category_default)

                if request_args.out_format == FORMAT_MWG:
                    f.write(create_mwg_out_format(ioc, request_args.mwg_type, headers_was_writen))
                    headers_was_writen = True

                if request_args.out_format in [FORMAT_JSON, FORMAT_XSOAR_JSON, FORMAT_JSON_SEQ, FORMAT_XSOAR_JSON_SEQ]:
                    xsoar = True if request_args.out_format in [FORMAT_XSOAR_JSON, FORMAT_XSOAR_JSON_SEQ] else False
                    f.write(json_format(list_fields, ioc, xsoar))

                if request_args.out_format == FORMAT_TEXT:
                    # save only the value and type of each indicator
                    f.write(str(json.dumps({"value": ioc.get("value"), "indicator_type": ioc.get("indicator_type")}))+"\n")

                if request_args.out_format == FORMAT_CSV:
                    f.write(csv_format(headers_was_writen, list_fields, ioc, request_args.fields_to_present))
                    headers_was_writen = True

    except Exception as e:
        demisto.debug(e)

    if request_args.out_format in [FORMAT_JSON, FORMAT_XSOAR_JSON, FORMAT_JSON_SEQ, FORMAT_XSOAR_JSON_SEQ]:
        ff = tempfile.TemporaryFile(mode='w+t')
        f.seek(2)
        ff.write('[' + f.read() + ']')
        f.close()
        return ff
    if request_args.out_format == FORMAT_PROXYSG:
        f = create_proxysg_all_category(f, files_by_category)
    return f


def json_format(list_fields, indicator, xsoar=False):
    filtered_json = {}
    if list_fields:
        for field in list_fields:
            value = indicator.get(field)
            if not value:
                value = indicator.get('CustomFields').get(field)
            filtered_json[field] = value
        indicator = filtered_json
    if not xsoar:
        json_format_indicator = {
            "indicator": indicator.get("value")
        }
        indicator.pop("value", None)
        json_format_indicator["value"] = indicator
        return ', ' + json.dumps(json_format_indicator)

    return ', ' + json.dumps(indicator)


def create_mwg_out_format(indicator: dict, mwg_type: str, headers_was_writen) -> str:
    if not indicator.get('value'):
        return ''
    value = "\"" + indicator.get('value') + "\""
    sources = indicator.get('sourceBrands')
    if sources:
        sources_string = "\"" + ','.join(sources) + "\""
    else:
        sources_string = "\"from CORTEX XSOAR\""

    if not headers_was_writen:
        if isinstance(mwg_type, list):
            mwg_type = mwg_type[0]
        return "type=" + mwg_type + "\n" + value + " " + sources_string + '\n'
    return value + " " + sources_string + '\n'


def create_proxysg_all_category(f, files_by_categry: dict):
    for category, category_file in files_by_categry.items():
        f.write(f"define category {category}\n")
        category_file.seek(0)
        f.write(category_file.read())
        category_file.close()
        f.write("end\n")

    return f


def create_proxysg_out_format(indicator: dict, files_by_category: dict, category_attribute: list, category_default: str = 'bc_category'):
    num_of_returned_indicators = 0

    if indicator.get('indicator_type') in ['URL', 'Domain', 'DomainGlob'] and indicator.get('value'):
        stripped_indicator = _PROTOCOL_REMOVAL.sub('', indicator.get('value'))
        indicator_proxysg_category = indicator.get('CustomFields', {}).get('proxysgcategory')
        # if a ProxySG Category is set and it is in the category_attribute list or that the attribute list is empty
        # than list add the indicator to it's category list
        if indicator_proxysg_category is not None and \
                (indicator_proxysg_category in category_attribute or len(category_attribute) == 0):
            files_by_category = add_indicator_to_category(stripped_indicator, indicator_proxysg_category,
                                                      files_by_category)
        else:
            # if ProxySG Category is not set or does not exist in the category_attribute list
            files_by_category = add_indicator_to_category(stripped_indicator, category_default, files_by_category)
        num_of_returned_indicators = 1

    return files_by_category, num_of_returned_indicators


def add_indicator_to_category(indicator, category, files_by_category):
    if category in files_by_category.keys():
        files_by_category[category].write(indicator + '\n')

    else:
        files_by_category[category] = tempfile.TemporaryFile(mode='w+t')
        files_by_category[category].write(indicator + '\n')

    return files_by_category


def csv_format(headers_was_writen, list_fields, ioc, headers):
    fields_value_list = []
    if not list_fields:
        values = list(ioc.values())
        if not headers_was_writen:
            headers = list(ioc.keys())
            headers_str = list_to_str(headers) + "\n"
            return headers_str + list_to_str(values, map_func=lambda val: f'"{val}"') + "\n"
        return list_to_str(values, map_func=lambda val: f'"{val}"') + "\n"
    else:
        for field in list_fields:
            value = ioc.get(field)
            if not value:
                value = ioc.get('CustomFields', {}).get(field)
            fields_value_list.append(value)
        if not headers_was_writen:
            headers_str = headers + '\n'
            return headers_str + list_to_str(fields_value_list, map_func=lambda val: f'"{val}"') + "\n"
        return list_to_str(fields_value_list, map_func=lambda val: f'"{val}"') + "\n"


def ip_groups_to_cidrs(ip_range_groups: Iterable):
    """Collapse ip groups list to CIDRs

    Args:
        ip_range_groups (Iterable): an Iterable of lists containing connected IPs

    Returns:
        Set. a set of CIDRs.
    """
    ip_ranges = set()
    for cidr in ip_range_groups:
        # handle single ips
        if len(cidr) == 1:
            # CIDR with a single IP appears with "/32" suffix so handle them differently
            ip_ranges.add(str(cidr[0]))
            continue

        ip_ranges.add(str(cidr))

    return ip_ranges


def ip_groups_to_ranges(ip_range_groups: Iterable):
    """Collapse ip groups to ranges.

    Args:
        ip_range_groups (Iterable): a list of lists containing connected IPs

    Returns:
        Set. a set of Ranges.
    """
    ip_ranges = set()
    for group in ip_range_groups:
        # handle single ips
        if len(group) == 1:
            ip_ranges.add(str(group[0]))
            continue

        ip_ranges.add(str(group))

    return ip_ranges


def ips_to_ranges(ips: Iterable, collapse_ips: str):
    """Collapse IPs to Ranges or CIDRs.

    Args:
        ips (Iterable): a group of IP strings.
        collapse_ips (str): Whether to collapse to Ranges or CIDRs.

    Returns:
        Set. a list to Ranges or CIDRs.
    """

    if collapse_ips == COLLAPSE_TO_RANGES:
        ips_range_groups = IPSet(ips).iter_ipranges()
        return ip_groups_to_ranges(ips_range_groups)

    else:
        cidrs = IPSet(ips).iter_cidrs()
        return ip_groups_to_cidrs(cidrs)


def list_to_str(inp_list: list, delimiter: str = ',', map_func: Callable = str) -> str:
    """
    Transforms a list to an str, with a custom delimiter between each list item
    """
    str_res = ""
    if inp_list:
        if isinstance(inp_list, list):
            str_res = delimiter.join(map(map_func, inp_list))
        else:
            raise AttributeError('Invalid inp_list provided to list_to_str')
    return str_res


# def get_mimetype(request_args):
#     return


def text_format(iocs, request_args: RequestArguments) -> Union[
    IO, IO[str]]:
    """
    Create a list result of formatted_indicators
     * Empty list:
         1) if add_comment_if_empty, return {'# Empty EDL'}
     * IP / CIDR:
         1) if collapse_ips, collapse IPs/CIDRs
     * URL:
         1) if drop_invalids, drop invalids (length > 254 or has invalid chars)
    * Other indicator types:
        1) if drop_invalids, drop invalids (has invalid chars)
        2) if port_stripping, strip ports
    """
    ipv4_formatted_indicators = set()
    ipv6_formatted_indicators = set()
    iocs.seek(0)
    formatted_indicators = tempfile.TemporaryFile(mode='w+t')
    for str_ioc in iocs:
        ioc = json.loads(str_ioc.rstrip())
        indicator = ioc.get('value')
        if not indicator:
            continue
        ioc_type = ioc.get('indicator_type')
        # protocol stripping
        if request_args.url_protocol_stripping:
            indicator = _PROTOCOL_REMOVAL.sub('', indicator)

        if ioc_type not in [FeedIndicatorType.IP, FeedIndicatorType.IPv6,
                            FeedIndicatorType.CIDR, FeedIndicatorType.IPv6CIDR]:
            indicator_without_port = _PORT_REMOVAL.sub(_URL_WITHOUT_PORT, indicator)
            if request_args.url_port_stripping:
                # remove port from indicator - from demisto.com:369/rest/of/path -> demisto.com/rest/of/path
                indicator = indicator_without_port
            # check if removing the port changed something about the indicator
            elif indicator != indicator_without_port and request_args.drop_invalids:
                # if port was in the indicator and url_port_stripping param not set - ignore the indicator
                continue
            # Reformatting to PAN-OS URL format
            with_invalid_tokens_indicator = indicator
            # mix of text and wildcard in domain field handling
            indicator = _INVALID_TOKEN_REMOVAL.sub('*', indicator)
            # check if the indicator held invalid tokens
            if request_args.drop_invalids:
                if with_invalid_tokens_indicator != indicator:
                    # invalid tokens in indicator - ignore the indicator
                    continue
                if ioc_type == FeedIndicatorType.URL and len(indicator) >= PAN_OS_MAX_URL_LEN:
                    # URL indicator exceeds allowed length - ignore the indicator
                    continue

            # for PAN-OS *.domain.com does not match domain.com
            # we should provide both
            # this could generate more than num entries according to PAGE_SIZE
            if indicator.startswith('*.'):
                # formatted_indicators.add(indicator.lstrip('*.'))
                formatted_indicators.write(str(indicator.lstrip('*.'))+'/n')

        if request_args.collapse_ips != DONT_COLLAPSE and ioc_type in (FeedIndicatorType.IP, FeedIndicatorType.CIDR):
            ipv4_formatted_indicators.add(indicator)

        elif request_args.collapse_ips != DONT_COLLAPSE and ioc_type == FeedIndicatorType.IPv6:
            ipv6_formatted_indicators.add(indicator)

        else:
            formatted_indicators.write(str(indicator)+'\n')

    if len(ipv4_formatted_indicators) > 0:
        ipv4_formatted_indicators = ips_to_ranges(ipv4_formatted_indicators, request_args.collapse_ips)
        for ip in ipv4_formatted_indicators:
            formatted_indicators.write(str(ip)+'\n')

    if len(ipv6_formatted_indicators) > 0:
        ipv6_formatted_indicators = ips_to_ranges(ipv6_formatted_indicators, request_args.collapse_ips)
        for ip in ipv6_formatted_indicators:
            formatted_indicators.write(str(ip)+'\n')

    return formatted_indicators


def get_outbound_mimetype(request_args: RequestArguments) -> str:
    """Returns the mimetype of the export_iocs"""
    if request_args.out_format == [FORMAT_JSON, FORMAT_XSOAR_JSON]:
        return MIMETYPE_JSON

    elif request_args.out_format == FORMAT_CSV and not request_args.csv_text:
        return MIMETYPE_CSV

    elif request_args.out_format in [FORMAT_JSON_SEQ, FORMAT_XSOAR_JSON_SEQ]:
        return MIMETYPE_JSON_SEQ

    else:
        return MIMETYPE_TEXT


def get_edl_on_demand():
    """
    Use the local file system to store the on-demand result, using a lock to
    limit access to the file from multiple threads.
    """
    ctx = get_integration_context()
    if EDL_ON_DEMAND_KEY in ctx:
        ctx.pop(EDL_ON_DEMAND_KEY, None)
        request_args = RequestArguments.from_context_json(ctx)
        edl = create_new_edl(request_args)
        with open(EDL_ON_DEMAND_CACHE_PATH, 'w') as file:
            file.write(edl)
        set_integration_context(ctx)
    else:
        with open(EDL_ON_DEMAND_CACHE_PATH, 'r') as file:
            edl = file.read()
    return edl


def validate_basic_authentication(headers: dict, username: str, password: str) -> bool:
    """
    Checks whether the authentication is valid.
    :param headers: The headers of the http request
    :param username: The integration's username
    :param password: The integration's password
    :return: Boolean which indicates whether the authentication is valid or not
    """
    credentials: str = headers.get('Authorization', '')
    if not credentials or 'Basic ' not in credentials:
        return False
    encoded_credentials: str = credentials.split('Basic ')[1]
    credentials: str = b64decode(encoded_credentials).decode('utf-8')
    if ':' not in credentials:
        return False
    credentials_list = credentials.split(':')
    if len(credentials_list) != 2:
        return False
    user, pwd = credentials_list
    return user == username and pwd == password


def get_bool_arg_or_param(args: dict, params: dict, key: str):
    val = args.get(key)
    return val.lower() == 'true' if isinstance(val, str) else params.get(key, False)


''' ROUTE FUNCTIONS '''


@APP.route('/', methods=['GET'])
def route_edl() -> Response:
    """
    Main handler for values saved in the integration context
    """
    params = demisto.params()

    credentials = params.get('credentials') if params.get('credentials') else {}
    username: str = credentials.get('identifier', '')
    password: str = credentials.get('password', '')
    cache_refresh_rate: str = params.get('cache_refresh_rate')
    if username and password:
        headers: dict = cast(Dict[Any, Any], request.headers)
        if not validate_basic_authentication(headers, username, password):
            err_msg: str = 'Basic authentication failed. Make sure you are using the right credentials.'
            demisto.debug(err_msg)
            return Response(err_msg, status=401, mimetype='text/plain', headers=[
                ('WWW-Authenticate', 'Basic realm="Login Required"'),
            ])

    request_args = get_request_args(request.args, params)
    on_demand = params.get('on_demand')
    created = datetime.now(timezone.utc)
    edl = get_edl_on_demand() if on_demand else create_new_edl(request_args)
    etag = f'"{hashlib.sha1(edl.encode()).hexdigest()}"'  # guardrails-disable-line
    query_time = (datetime.now(timezone.utc) - created).total_seconds()
    edl_size = 0
    if edl.strip():
        edl_size = edl.count('\n') + 1  # add 1 as last line doesn't have a \n
    if len(edl) == 0 and request_args.add_comment_if_empty:
        edl = '# Empty EDL'
    mimetype = get_outbound_mimetype(request_args)
    max_age = ceil((datetime.now() - dateparser.parse(cache_refresh_rate)).total_seconds())  # type: ignore[operator]
    demisto.debug(f'Returning edl of size: [{edl_size}], created: [{created}], query time seconds: [{query_time}],'
                  f' max age: [{max_age}], etag: [{etag}]')
    resp = Response(edl, status=200, mimetype=mimetype, headers=[
        ('X-EDL-Created', created.isoformat()),
        ('X-EDL-Query-Time-Secs', "{:.3f}".format(query_time)),
        ('X-EDL-Size', str(edl_size)),
        ('ETag', etag),
    ])
    resp.cache_control.max_age = max_age
    resp.cache_control[
        'stale-if-error'] = '600'  # number of seconds we are willing to serve stale content when there is an error
    return resp


def get_request_args(request_args: dict, params: dict) -> RequestArguments:
    """
    Processing a flask request arguments and generates a RequestArguments instance from it.
    Args:
        request_args: Flask request arguments
        params: Integration configuration parameters

    Returns:
        RequestArguments instance with processed arguments
    """
    limit = try_parse_integer(request_args.get('n', params.get('edl_size') or 10000), EDL_LIMIT_ERR_MSG)
    offset = try_parse_integer(request_args.get('s', 0), EDL_OFFSET_ERR_MSG)
    out_format = request.args.get('v', params.get('format', 'text'))
    query = request_args.get('q', params.get('indicators_query') or '')
    mwg_type = request.args.get('t', params.get('mwg_type', "string"))
    strip_port = request_args.get('sp', params.get('url_port_stripping') or False)
    strip_protocol = request_args.get('pr', params.get('url_protocol_stripping') or False)
    drop_invalids = request_args.get('di', params.get('drop_invalids') or False)
    category_default = request.args.get('cd', params.get('category_default', 'bc_category'))
    category_attribute = request.args.get('ca', params.get('category_attribute', ''))
    collapse_ips = request_args.get('tr', params.get('collapse_ips', DONT_COLLAPSE))
    csv_text = request.args.get('tx', params.get('csv_text', False))
    sort_field = request.args.get('sf', params.get('sort_field'))
    sort_order = request.args.get('so', params.get('sort_order'))
    add_comment_if_empty = request_args.get('ce', params.get('add_comment_if_empty', True))

    fields_to_present = request.args.get('f', params.get('fields_filter', ''))

    # handle flags
    if drop_invalids == '':
        drop_invalids = True

    if strip_port == '':
        strip_port = True

    if strip_protocol == '':
        strip_port = True

    if collapse_ips not in [DONT_COLLAPSE, COLLAPSE_TO_CIDR, COLLAPSE_TO_RANGES]:
        collapse_ips = try_parse_integer(collapse_ips, EDL_COLLAPSE_ERR_MSG)

        if collapse_ips not in [0, 1, 2]:
            raise DemistoException(EDL_COLLAPSE_ERR_MSG)

        collapse_options = {
            0: DONT_COLLAPSE,
            1: COLLAPSE_TO_RANGES,
            2: COLLAPSE_TO_CIDR
        }
        collapse_ips = collapse_options[collapse_ips]
    if out_format not in [FORMAT_PROXYSG, FORMAT_TEXT, FORMAT_JSON, FORMAT_CSV,
                          FORMAT_JSON_SEQ, FORMAT_MWG, FORMAT_ARG_BLUECOAT, FORMAT_ARG_MWG,
                          FORMAT_ARG_PROXYSG, FORMAT_XSOAR_JSON, FORMAT_ARG_XSOAR_JSON,
                          FORMAT_XSOAR_JSON_SEQ, FORAMT_ARG_XSOAR_JSON_SEQ]:
        raise DemistoException(EDL_FORMAT_ERR_MSG)

    elif out_format in [FORMAT_ARG_PROXYSG, FORMAT_ARG_BLUECOAT]:
        out_format = FORMAT_PROXYSG

    elif out_format == FORMAT_ARG_MWG:
        out_format = FORMAT_MWG

    elif out_format == FORMAT_ARG_XSOAR_JSON:
        out_format = FORMAT_XSOAR_JSON

    elif out_format == FORAMT_ARG_XSOAR_JSON_SEQ:
        out_format = FORMAT_XSOAR_JSON_SEQ

    if out_format == FORMAT_MWG:
        if mwg_type not in MWG_TYPE_OPTIONS:
            raise DemistoException(EDL_MWG_TYPE_ERR_MSG)

    if params.get('use_legacy_query'):
        # workaround for "msgpack: invalid code" error
        fields_to_present = 'use_legacy_query'

    return RequestArguments(query,
                            out_format,
                            limit,
                            offset,
                            strip_port,
                            drop_invalids,
                            collapse_ips,
                            add_comment_if_empty,
                            mwg_type,
                            category_default,
                            category_attribute,
                            fields_to_present,
                            csv_text,
                            sort_field,
                            sort_order,
                            strip_protocol
                            )


''' COMMAND FUNCTIONS '''


def test_module(_: Dict, params: Dict):
    """
    Validates:
        1. Valid port.
        2. Valid cache_refresh_rate
    """
    get_params_port(params)
    on_demand = params.get('on_demand', None)
    if not on_demand:
        try_parse_integer(params.get('edl_size'), EDL_LIMIT_ERR_MSG)  # validate EDL Size was set
        cache_refresh_rate = params.get('cache_refresh_rate', '')
        if not cache_refresh_rate:
            raise ValueError(EDL_MISSING_REFRESH_ERR_MSG)
        # validate cache_refresh_rate value
        range_split = cache_refresh_rate.split(' ')
        if len(range_split) != 2:
            raise ValueError(EDL_MISSING_REFRESH_ERR_MSG)
        try_parse_integer(range_split[0], 'Invalid time value for the Refresh Rate. Must be a valid integer.')
        if not range_split[1] in ['minute', 'minutes', 'hour', 'hours', 'day', 'days', 'month', 'months', 'year',
                                  'years']:
            raise ValueError(
                'Invalid time unit for the Refresh Rate. Must be minutes, hours, days, months, or years.')
        parse_date_range(cache_refresh_rate, to_timestamp=True)
    run_long_running(params, is_test=True)
    return 'ok', {}, {}


def update_edl_command(args: Dict, params: Dict):
    """
    Updates the context to update the EDL values on demand the next time it runs
    """
    on_demand = params.get('on_demand')
    if not on_demand:
        raise DemistoException(
            '"Update EDL On Demand" is off. If you want to update the EDL manually please toggle it on.')
    limit = try_parse_integer(args.get('edl_size', params.get('edl_size')), EDL_LIMIT_ERR_MSG)
    query = args.get('query', '')
    collapse_ips = args.get('collapse_ips', DONT_COLLAPSE)
    url_port_stripping = get_bool_arg_or_param(args, params, 'url_port_stripping')
    strip_protocol = get_bool_arg_or_param(args, params, 'url_protocol_stripping')
    drop_invalids = get_bool_arg_or_param(args, params, 'drop_invalids')
    add_comment_if_empty = get_bool_arg_or_param(args, params, 'add_comment_if_empty')
    offset = try_parse_integer(args.get('offset', 0), EDL_OFFSET_ERR_MSG)
    mwg_type = params.get('mwg_type', "string")
    category_default = params.get('category_default', 'bc_category')
    category_attribute = params.get('category_attribute', '')
    fields_to_present = params.get('fields_filter', '')
    out_format = params.get('format', 'text')
    csv_text = args.get('csv_text') == 'True'
    sort_field = args.get('sort_field')
    sort_order = args.get('sort_order')

    if params.get('use_legacy_query'):
        # workaround for "msgpack: invalid code" error
        fields_to_present = 'use_legacy_query'

    request_args = RequestArguments(query,
                                    out_format,
                                    limit,
                                    offset,
                                    url_port_stripping,
                                    drop_invalids,
                                    collapse_ips,
                                    add_comment_if_empty,
                                    mwg_type,
                                    category_default,
                                    category_attribute,
                                    fields_to_present,
                                    csv_text,
                                    sort_field,
                                    sort_order,
                                    strip_protocol)

    ctx = request_args.to_context_json()
    ctx[EDL_ON_DEMAND_KEY] = True
    set_integration_context(ctx)
    hr = 'EDL will be updated the next time you access it'
    return hr, {}, {}


def initialize_edl_context(params: dict):
    global EDL_ON_DEMAND_CACHE_PATH
    limit = try_parse_integer(params.get('edl_size'), EDL_LIMIT_ERR_MSG)
    query = params.get('indicators_query', '')
    collapse_ips = params.get('collapse_ips', DONT_COLLAPSE)
    url_port_stripping = params.get('url_port_stripping', False)
    url_protocol_stripping = params.get('url_port_stripping', False)
    drop_invalids = params.get('drop_invalids', False)
    add_comment_if_empty = params.get('add_comment_if_empty', True)
    mwg_type = params.get('mwg_type', "string")
    category_default = params.get('category_default', 'bc_category')
    category_attribute = params.get('category_attribute', '')
    fields_to_present = params.get('fields_filter', '')
    out_format = params.get('format', 'text')
    csv_text = params.get('csv_text') == 'True'
    sort_field = params.get('sort_field')
    sort_order = params.get('sort_order')
    if params.get('use_legacy_query'):
        # workaround for "msgpack: invalid code" error
        fields_to_present = 'use_legacy_query'
    offset = 0
    request_args = RequestArguments(query,
                                    out_format,
                                    limit,
                                    offset,
                                    url_port_stripping,
                                    drop_invalids,
                                    collapse_ips,
                                    add_comment_if_empty,
                                    mwg_type,
                                    category_default,
                                    category_attribute,
                                    fields_to_present,
                                    csv_text,
                                    sort_field,
                                    sort_order,
                                    url_protocol_stripping)

    EDL_ON_DEMAND_CACHE_PATH = demisto.uniqueFile()
    ctx = request_args.to_context_json()
    ctx[EDL_ON_DEMAND_KEY] = True
    set_integration_context(ctx)


def main():
    """
    Main
    """
    global PAGE_SIZE
    params = demisto.params()
    try:
        PAGE_SIZE = max(1, int(params.get('page_size') or PAGE_SIZE))
    except ValueError:
        demisto.debug(f'Non integer "page_size" provided: {params.get("page_size")}. defaulting to {PAGE_SIZE}')
    credentials = params.get('credentials') if params.get('credentials') else {}
    username: str = credentials.get('identifier', '')
    password: str = credentials.get('password', '')
    if (username and not password) or (password and not username):
        err_msg: str = 'If using credentials, both username and password should be provided.'
        demisto.debug(err_msg)
        raise DemistoException(err_msg)
    command = demisto.command()
    demisto.debug(f'Command being called is {command}')
    commands = {
        'test-module': test_module,
        'edl-update': update_edl_command,
    }

    try:
        initialize_edl_context(params)
        if command == 'long-running-execution':
            run_long_running(params)
        elif command in commands:
            readable_output, outputs, raw_response = commands[command](demisto.args(), params)
            return_outputs(readable_output, outputs, raw_response)
        else:
            raise NotImplementedError(f'Command "{command}" is not implemented.')
    except Exception as e:
        err_msg = f'Error in {INTEGRATION_NAME} Integration [{e}]'
        return_error(err_msg)


from NGINXApiModule import *  # noqa: E402


if __name__ in ['__main__', '__builtin__', 'builtins']:
    main()
