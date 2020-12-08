import collections
import json
import logging
import os
import re
from functools import lru_cache

from bs4 import BeautifulSoup

from xbrlassembler.enums import XBRLType, FinancialStatement, DateParser
from xbrlassembler.error import XBRLError

logger = logging.getLogger('xbrlassembler')


class XBRLElement:
    """
    An element to represent a single point on a tree.
    Specific data values are geared towards xbrl relevent information
    While relational data is close to that of an XML tree element

    Args:
        :param uri: A unique identifier for this specific point

    Kwargs:
        :param label: Printable and readable identifier
        :param value: Data that sits on a specific point, mostly used for elements at the bottem of the tree
        :param ref: Reference data that gives context to the value
    """

    def __init__(self, uri, label=None, value=None, ref=None):
        """Constructor Method"""
        self.uri = uri.split("/")[-1]
        self.label = label
        self.ref = ref
        self.value = value

        self.date = None

        # Convert to float to remove any issues with comparing string representations of numbers
        try:
            self.value = float(value)
        except (TypeError, ValueError):
            if isinstance(value, str):
                self.value = value.replace('\n', '')

        self.children = {}
        self.parent = None

    def __repr__(self):
        """
        :return: Returns a string representation of various aspects of the non relational data
        """
        return f"{self.uri} (label={self.label}, ref={self.ref}, value={self.value})"

    @lru_cache(maxsize=512)
    def head(self) -> "XBRLElement":
        """
        Return the top element of the tree
        :return: XBRLElement
        """
        return self if self.parent is None else self.parent.head()

    def add_child(self, child, order=-1):
        """
        Essential function for establishing relationships between elements.
        This function ensures that the relationship is set on both parent and child
        elements without duplication or Nones

        Args:
            :param child: An XBRLElement that is going to be under this element in the tree
            :param order: An optional argument to add order to child elements
        """
        if not isinstance(child, XBRLElement):
            return

        for already_child in self.children:
            if already_child.uri == child.uri and already_child.ref == child.ref:
                self.merge(child)
                return

        try:
            order = int(float(order))
        except Exception as e:
            logger.info(f"order to float to int failed {order}, {e}")
            return

        self.children[child] = order
        child.parent = self

    def merge(self, other):
        """
        Attempts to merge one XBRLElement with another resulting in one element with more complete information
        :param other: An `XBRLElement` to be absorbed
        :return:
        """
        self.label = self.label or other.label
        self.value = self.value or other.value
        self.parent = self.parent or other.parent

        for new_child, order in other.children.items():
            self.add_child(new_child, order)

    @lru_cache(maxsize=512)
    def visualize(self) -> str:
        """
        A function to create a printable representation of the tree from this point
        :return: A multiline string
        """
        vis = f"\n{self.__repr__()}"
        if self.children:
            for child in self.children:
                vis += child.visualize().replace('\n', '\n\t')
        return vis

    @lru_cache(maxsize=512)
    def refs(self) -> dict:
        """
        A quick utility function to pull and parse all bottom level references in the tree
        :return: A dict mapping old references to parsed ones
        """
        if self.date is None:
            self.date = DateParser.parse(self.ref)

        ref_map = {self.ref: self.date}
        for child in self.children:
            ref_map.update(child.refs())
        return ref_map

    @lru_cache(maxsize=512)
    def ids(self) -> dict:
        """
        Recursive function to access all uri label pairs
        :return: A dictionary where keys are uri strings and values are label strings or None is there is no label
        """
        ids = {self.uri: self.label}
        for child in self.children:
            ids.update(child.ids())
        return ids

    @lru_cache(maxsize=512)
    def search(self, **kwargs) -> "XBRLElement":
        """
        A search function to find specific node that has a value that matches any of the kwargs
        :param term: String, re.pattern, or anything that can go into a search
        :return: A specific node from the tree
        """
        smap = self.__dict__
        for x, v in kwargs.items():
            if x in smap.keys() and v is not None:
                if smap[x] is not None:
                    if re.search(v, smap[x]):
                        return self

        for child in self.children:
            child_search = child.search(**kwargs)
            if child_search:
                return child_search

    @lru_cache(maxsize=512)
    def items(self):
        """
        A recursive function iterator allowing access to loop over the entire dataset as a list
        :return: Yields  Uri, Label, Ref, Value
        """
        yield self
        for child in self.children.keys():
            for ele in child.items():
                yield ele

    @lru_cache(maxsize=512)
    def data(self):
        """
        A recursive function iterator returning all low level elements
        :return: Yields XBRLElement
        """
        if all(child.value is not None and len(child.children) == 0 for child in self.children):
            yield self

        for child in self.children.keys():
            for ele in child.data():
                yield ele

    @lru_cache(maxsize=512)
    def to_json(self) -> dict:
        """
        Creates a json representation of the tree
        :return: A dictionary representation of the tree
        """
        json_data = {'u': self.uri, 'l': self.label, 'r': self.ref, 'v': self.value, 'c': []}
        for child in self.children:
            json_data['c'].append(child.to_json())
        return json_data

    @classmethod
    def from_json(cls, data):
        """
        Creates an XBRLElement tree from json data
        :param data: A dict of data loaded from a json file
        :return:
        """
        element = cls(uri=data['u'], label=data['l'], ref=data['r'], value=data['v'])
        for child_data in data['c']:
            element.add_child(cls.from_json(child_data))
        return element


class XBRLAssembler:
    """
    XBRLAssembler is a data object that is comprised of a map of trees which represent various financial statements.
    The primary functionality of this class is for loading and saving data, but also selecting specific data trees.
    """

    def __init__(self):
        self.xbrl_elements = {}

    def __repr__(self):
        return self.xbrl_elements.__repr__()

    @classmethod
    def _init(cls, file_map, ref_doc):
        """
        Protected method to turn semi-organized xbrl data into a XBRLAssembler object.
        This is required as the from_json constructor will populate the object without
        needing any parsing so parsing can't happen in the __init__ function.

        :param file_map: A dict of `XBRLType` and `BeautifulSoup` objects
        :param ref_doc: A `XBRLType` to specify the reference document to use
        :return: A complete `XBRLAssembler` with compiled data
        """
        schema = file_map[XBRLType.SCHEMA]
        if not isinstance(schema, BeautifulSoup):
            raise XBRLError(f"XBRLAssembler schema requires a BeautifulSoup not {schema}")

        label = file_map[XBRLType.LABEL]
        if not isinstance(label, BeautifulSoup):
            raise XBRLError(f"XBRLAssembler label requires a BeautifulSoup not {label}")

        cell = file_map[XBRLType.DATA]
        if not isinstance(cell, BeautifulSoup):
            raise XBRLError(f"XBRLAssembler cell requires a BeautifulSoup not {cell}")

        ref_type = next(ref for ref in {ref_doc, XBRLType.PRE, XBRLType.DEF, XBRLType.CALC} if ref in file_map)
        ref = file_map[ref_type]
        if not isinstance(ref, BeautifulSoup):
            raise XBRLError(f"XBRLAssembler ref requires a BeautifulSoup not {ref}")

        assembler = cls()
        assembler.parse_schema(schema)
        assembler.parse_ref(labels=assembler.parse_labels(label),
                            cells=assembler.parse_cells(cell),
                            ref_soup=ref)

        return assembler

    @classmethod
    def from_dir(cls, directory, ref_doc=XBRLType.PRE):
        """
        Alternative constructor that will attempt to search the specific directory for a set of xbrl documents

        :param directory: A string to a directory that will be scanned for xbrl documents
        :param ref_doc: Optional class`xbrlassembler.XBRLType` used to specify the requested reference document

        :return: A class:`xbrlassembler.XBRLAssembler`
        """
        if not os.path.isdir(directory):
            raise XBRLError(f"{directory} is not a valid directory")

        file_map = {}
        for item in os.listdir(directory):
            if re.search(r'.*\.(xml|xsd)', item):
                xbrl_type = XBRLType.get(item)
                if xbrl_type:
                    file_map[xbrl_type] = BeautifulSoup(open(os.path.join(directory, item), 'r'), 'lxml')

        try:
            return cls._init(file_map=file_map, ref_doc=ref_doc)
        except KeyError as e:
            raise XBRLError(f"Error creating XBRLAssembler from {directory} {e.__repr__()}")

    @classmethod
    def from_json(cls, file_path):
        """
        Creates a XBRLAssembler from a json file.
        The file should have been created from the `XBRLAssembler.to_json()` function

        :param file_path: A string file path
        :return: A `XBRLAssembler`
        """
        if not isinstance(file_path, str):
            raise TypeError(f"XBRLAssembler.from_json needs a file_path string not {file_path}")

        xbrl_assembler = cls()

        with open(file_path, 'r') as file:
            data_dict = {uri: XBRLElement.from_json(ele) for uri, ele in json.load(file).items()}

        xbrl_assembler.xbrl_elements.update(data_dict)

        return xbrl_assembler

    def to_json(self, file_path, mode='w+'):
        """
        A write function to store all data in a json file
        :param file_path: A string to a file
        :param mode: mode string for open
        """
        with open(file_path, mode) as file:
            file.write(json.dumps({uri: ele.to_json() for uri, ele in self.xbrl_elements.items()}, indent=4))

    def merge(self, *others):
        """
        Attempts to merge an `XBRLAssembler` with another `XBRLAssembler`
        The merge is aimed to take bottom level elements of other trees and match them
        with bottom level elements of existing trees.

        :param others: One or many `XBRLAssemblers`
        """
        for other in others:
            if other is self:
                continue

            if not isinstance(other, XBRLAssembler):
                raise XBRLError(f"XBRLAssembler must merge with another XBRLAssembler not {type(other)}")

            for uri, header_ele in self.xbrl_elements.items():
                def search_check(regex, ele):
                    return re.search(regex, ele.uri) or re.search(regex, ele.label)

                fin_stmt = next((stmt for stmt in FinancialStatement if search_check(stmt.value, header_ele)), None)
                if fin_stmt == FinancialStatement.NOTE:
                    continue
                other_doc = other.get(fin_stmt) if fin_stmt is not None else other.get(header_ele.uri)

                if other_doc is None:
                    logger.debug(f"Merge failed on document search {uri}")
                    continue

                for other_ele in other_doc.data():
                    search_ele = header_ele.search(uri=other_ele.uri, label=other_ele.label)
                    if search_ele:
                        search_ele.merge(other_ele)
                    else:
                        logger.debug(f"Merge failed on element search "
                                     f"(header_ele={header_ele.uri}, other_ele={other_ele})")

    def parse_schema(self, schema_soup):
        """
        Parsing function for XBRL schema and adding it to the XBRLAssembler top level elements

        This establishes the access point for other documents as URI's from this find relevent data in the
        reference document
        :param schema_soup: A `BeautifulSoup` object
        :return:
        """
        for role_type in schema_soup.find_all("link:roletype"):
            uri = role_type['roleuri']
            label = role_type.find("link:definition").text
            if "Parenthetical" not in label:  # "Statement" in label and
                text = label.split(" - ")
                ele = XBRLElement(uri=uri, label=text[-1], ref=text[0])
                self.xbrl_elements[uri] = ele

    @staticmethod
    def parse_labels(label_soup):
        """
        Parsing function for XBRL label file to provide readable labels to all elements
        :param label_soup: A `BeautifulSoup` object
        :return: A dict of labels
        """

        def uri_search(raw):
            uri_re = re.compile(r'(?:lab_)?((us-gaap|source|dei|[a-z]{3,4})[_:][A-Za-z]{5,})', re.IGNORECASE)
            uris = re.search(uri_re, raw)
            return uris.group(1) if uris else raw

        labels = {}
        for lab in label_soup.find_all(re.compile('label$', re.IGNORECASE)):
            try:
                u = uri_search(lab['xlink:label']).lower()
                labels[u if u != lab['xlink:label'] else uri_search(lab['id'])] = lab.text
            except KeyError:
                continue

        return labels

    @staticmethod
    def parse_cells(data_soup):
        """
        Parsing function for the base XML data document for low level data
        :param data_soup: A `BeautifulSoup` object
        """
        cells = collections.defaultdict(list)
        for node in data_soup.find_all(attrs={"contextref": True}):
            uri = node.name.replace(':', '_')
            ele = XBRLElement(uri=uri,
                              value=node.text,
                              ref=node['contextref'])
            cells[uri].append(ele)
        return cells

    def parse_ref(self, labels, cells, ref_soup):
        """
        The combination tool of all xbrl documents.
        After all schema, labels, and cells are parsed the reference document is used to establish relationships.
        These relationships are then formed into a tree structure of `XBRLElement` creating financial statment trees.

        :param labels: A map of uri's to label strings
        :param cells: A map of uri's to a list of XBRLElements with values
        :param ref_soup: A `BeautifulSoup` object representing the reference document
        """
        for doc_uri, doc_ele in self.xbrl_elements.items():
            # Find desired section in reference document
            def_link = ref_soup.find(re.compile(r'link', re.IGNORECASE), attrs={'xlink:role': doc_uri})
            if not def_link:
                continue

            # Pull all elements and create XBRLElements out of them
            eles = {}
            references = collections.defaultdict(int)
            for loc in def_link.find_all(re.compile(r'loc', re.IGNORECASE)):
                uri = loc['xlink:href'].split('#')[1]
                label = labels[uri.lower()] if uri.lower() in labels else None
                ele = XBRLElement(uri=uri, label=label)
                eles[loc['xlink:label']] = ele

                if ele.uri.lower() in cells:
                    for cell in cells[ele.uri.lower()]:
                        references[cell.ref] += 1

            if not references:
                continue

            # Find and create parent/child relationships between new elements
            for arc in def_link.find_all(re.compile(r'\w*arc', re.IGNORECASE)):
                parent, child, order = eles[arc['xlink:from']], eles[arc['xlink:to']], arc['order']
                parent.add_child(child=child, order=order)

            # Clean out incorrect refences
            most_used = max(references.values())
            references = set(ref for ref, count in references.items() if count == most_used)

            # Determine top and bottom level elements in the document (put under header or fill in cells)
            for order, ele in enumerate(eles.values()):
                if ele.parent is None:
                    doc_ele.add_child(child=ele, order=order)

                if ele.uri.lower() in cells:
                    possible_cells = cells[ele.uri.lower()]

                    for cell in possible_cells:
                        if cell.ref in references:
                            ele.add_child(cell)

    def get(self, search) -> XBRLElement:
        """
        Main access function that will take a variety of search criteria and attempt to create and
            return the document tree relevent to the search

        :param search: Regex, string, or FinancialStatement enum to search with

        :return: class:`xbrlassembler.XBRLElement` for the top of a tree representing the requested document
        """
        search_data = sorted(self.xbrl_elements.values(), key=lambda item: item.ref)

        if isinstance(search, re.Pattern) or isinstance(search, str):
            search_term = search
        elif isinstance(search, FinancialStatement):
            search_term = search.value
        else:
            raise ValueError(f"XBRLAssembler.get() search term should be "
                             f"re.Pattern, string, or FinancialStatement not {search}")

        def doc_search(term, ele):
            return re.search(term, ele.uri) or re.search(term, ele.label)

        return next((ele for ele in search_data if doc_search(search_term, ele)), None)
