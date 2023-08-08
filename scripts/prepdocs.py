import os
import argparse
import glob
import html
import io
import re
import time
from pypdf import PdfReader, PdfWriter
from azure.identity import AzureDeveloperCliCredential
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import *
from azure.search.documents import SearchClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup

MAX_SECTION_LENGTH = 1000
SENTENCE_SEARCH_LIMIT = 100
SECTION_OVERLAP = 100

parser = argparse.ArgumentParser(
    description="Prepare documents by extracting content from PDFs, splitting content into sections, uploading to blob storage, and indexing in a search index.",
    epilog="Example: prepdocs.py '..\data\*' --storageaccount myaccount --container mycontainer --searchservice mysearch --index myindex -v"
    )
parser.add_argument("files", help="Files to be processed")
parser.add_argument("--category", help="Value for the category field in the search index for all sections indexed in this run")
parser.add_argument("--skipblobs", action="store_true", help="Skip uploading individual pages to Azure Blob Storage")
parser.add_argument("--storageaccount", help="Azure Blob Storage account name")
parser.add_argument("--container", help="Azure Blob Storage container name")
parser.add_argument("--storagekey", required=False, help="Optional. Use this Azure Blob Storage account key instead of the current user identity to login (use az login to set current user for Azure)")
parser.add_argument("--tenantid", required=False, help="Optional. Use this to define the Azure directory where to authenticate)")
parser.add_argument("--searchservice", help="Name of the Azure Cognitive Search service where content should be indexed (must exist already)")
parser.add_argument("--index", help="Name of the Azure Cognitive Search index where content should be indexed (will be created if it doesn't exist)")
parser.add_argument("--searchkey", required=False, help="Optional. Use this Azure Cognitive Search account key instead of the current user identity to login (use az login to set current user for Azure)")
parser.add_argument("--remove", action="store_true", help="Remove references to this document from blob storage and the search index")
parser.add_argument("--removeall", action="store_true", help="Remove all blobs from blob storage and documents from the search index")
parser.add_argument("--localpdfparser", action="store_true", help="Use PyPdf local PDF parser (supports only digital PDFs) instead of Azure Form Recognizer service to extract text, tables and layout from the documents")
parser.add_argument("--formrecognizerservice", required=False, help="Optional. Name of the Azure Form Recognizer service which will be used to extract text, tables and layout from the documents (must exist already)")
parser.add_argument("--formrecognizerkey", required=False, help="Optional. Use this Azure Form Recognizer account key instead of the current user identity to login (use az login to set current user for Azure)")
parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
args = parser.parse_args()

# TODO: Read from arguments
# urls = ["www.dnb.no/forsikring/bilforsikring", "www.dnb.no/forsikring", "www.dnb.no/forsikring/husforsikring", "www.dnb.no/forsikring/innboforsikring", "www.dnb.no/forsikring/reiseforsikring", "www.dnb.no/forsikring/personforsikring", "www.dnb.no/forsikring/meld-skade", "www.dnb.no/forsikring/rabatt", "www.dnb.no/forsikring/best-i-test-forsikring", "www.dnb.no/forsikring/fremtind", "www.dnb.no/forsikring/verdisakforsikring", "www.dnb.no/forsikring/verdisakforsikring/sykkelforsikring", "www.dnb.no/forsikring/kjoretoy/sma-elektriske-kjoretoy", "www.dnb.no/forsikring/verdisakforsikring/bunadsforsikring", "www.dnb.no/forsikring/kjoretoy", "www.dnb.no/forsikring/kjoretoy/batforsikring", "www.dnb.no/forsikring/kjoretoy/motorsykkelforsikring", "www.dnb.no/forsikring/kjoretoy/bobilforsikring", "www.dnb.no/forsikring/kjoretoy/campingvognforsikring", "www.dnb.no/forsikring/kjoretoy/mopedforsikring", "www.dnb.no/forsikring/kjoretoy/snoscooterforsikring", "www.dnb.no/forsikring/kjoretoy/tilhengerforsikring", "dokument.fremtind.no/vilkar/fremtind/pm/mobilitet/Vilkar_ansvar_bil.pdf", "dokument.fremtind.no/vilkar/fremtind/pm/mobilitet/Vilkar_Minikasko_Bil.pdf", "dokument.fremtind.no/vilkar/fremtind/pm/mobilitet/Vilkar_Kasko_Bil.pdf", "dokument.fremtind.no/vilkar/fremtind/pm/mobilitet/Vilkar_Toppkasko_Bil.pdf", "dokument.fremtind.no/ipid/IPID_BIL.pdf"]
# urls = ["www.dnb.no/forsikring/bilforsikring", "www.dnb.no/forsikring", "www.dnb.no/forsikring/husforsikring", "www.dnb.no/forsikring/innboforsikring"]

url_sources = [("www.dnb.no/en/insurance/house-insurance", "house insurance"), ("www.dnb.no/en/insurance/home-contents-insurance", "content insurance"), ("www.dnb.no/en/insurance/car-insurance", "car insurance"), ("www.dnb.no/en/insurance", "general insurance information")]
file_sources = [("data/Car insurance.pdf", "car insurance"), ("data/HouseInsuranceTest.pdf", "house insurance"),  ("data/contentinsurance.pdf",  "content insurance")]

# Use the current user identity to connect to Azure services unless a key is explicitly set for any of them
azd_credential = AzureDeveloperCliCredential() if args.tenantid == None else AzureDeveloperCliCredential(tenant_id=args.tenantid, process_timeout=60)
default_creds = azd_credential if args.searchkey == None or args.storagekey == None else None
search_creds = default_creds if args.searchkey == None else AzureKeyCredential(args.searchkey)
if not args.skipblobs:
    storage_creds = default_creds if args.storagekey == None else args.storagekey
if not args.localpdfparser:
    # check if Azure Form Recognizer credentials are provided
    if args.formrecognizerservice == None:
        print("Error: Azure Form Recognizer service is not provided. Please provide formrecognizerservice or use --localpdfparser for local pypdf parser.")
        exit(1)
    formrecognizer_creds = default_creds if args.formrecognizerkey == None else AzureKeyCredential(args.formrecognizerkey)

def blob_name_from_file_page(filename, page = 0):
    if os.path.splitext(filename)[1].lower() == ".pdf":
        return os.path.splitext(os.path.basename(filename))[0] + f"-{page}" + ".pdf"
    else:
        return os.path.basename(filename)

def upload_blobs(filename):
    blob_service = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)
    blob_container = blob_service.get_container_client(args.container)
    if not blob_container.exists():
        blob_container.create_container()

    # if file is PDF split into pages and upload each page as a separate blob
    if os.path.splitext(filename)[1].lower() == ".pdf":
        reader = PdfReader(filename)
        pages = reader.pages
        for i in range(len(pages)):
            blob_name = blob_name_from_file_page(filename, i)
            if args.verbose: print(f"\tUploading blob for page {i} -> {blob_name}")
            f = io.BytesIO()
            writer = PdfWriter()
            writer.add_page(pages[i])
            writer.write(f)
            f.seek(0)
            blob_container.upload_blob(blob_name, f, overwrite=True)
    else:
        blob_name = blob_name_from_file_page(filename)
        with open(filename,"rb") as data:
            blob_container.upload_blob(blob_name, data, overwrite=True)

def remove_blobs(filename):
    if args.verbose: print(f"Removing blobs for '{filename or '<all>'}'")
    blob_service = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)
    blob_container = blob_service.get_container_client(args.container)
    if blob_container.exists():
        if filename == None:
            blobs = blob_container.list_blob_names()
        else:
            prefix = os.path.splitext(os.path.basename(filename))[0]
            blobs = filter(lambda b: re.match(f"{prefix}-\d+\.pdf", b), blob_container.list_blob_names(name_starts_with=os.path.splitext(os.path.basename(prefix))[0]))
        for b in blobs:
            if args.verbose: print(f"\tRemoving blob {b}")
            blob_container.delete_blob(b)

def table_to_html(table):
    table_html = "<table>"
    rows = [sorted([cell for cell in table.cells if cell.row_index == i], key=lambda cell: cell.column_index) for i in range(table.row_count)]
    for row_cells in rows:
        table_html += "<tr>"
        for cell in row_cells:
            tag = "th" if (cell.kind == "columnHeader" or cell.kind == "rowHeader") else "td"
            cell_spans = ""
            if cell.column_span > 1: cell_spans += f" colSpan={cell.column_span}"
            if cell.row_span > 1: cell_spans += f" rowSpan={cell.row_span}"
            table_html += f"<{tag}{cell_spans}>{html.escape(cell.content)}</{tag}>"
        table_html +="</tr>"
    table_html += "</table>"
    return table_html

def get_document_text_from_analysis_result(result: AnalyzeResult):
    offset = 0
    page_map = []
    for page_num, page in enumerate(result.pages):
        tables_on_page = [table for table in result.tables if table.bounding_regions[0].page_number == page_num + 1]

        # mark all positions of the table spans in the page
        page_offset = page.spans[0].offset
        page_length = page.spans[0].length
        table_chars = [-1]*page_length
        for table_id, table in enumerate(tables_on_page):
            for span in table.spans:
                # replace all table spans with "table_id" in table_chars array
                for i in range(span.length):
                    idx = span.offset - page_offset + i
                    if idx >=0 and idx < page_length:
                        table_chars[idx] = table_id

        # build page text by replacing charcters in table spans with table html
        page_text = ""
        added_tables = set()
        for idx, table_id in enumerate(table_chars):
            if table_id == -1:
                page_text += result.content[page_offset + idx]
            elif not table_id in added_tables:
                page_text += table_to_html(tables_on_page[table_id])
                added_tables.add(table_id)

        page_text += " "
        page_map.append((page_num, offset, page_text))
        offset += len(page_text)

    return page_map

def get_html_page_text(url):
    req = Request(f"https://{url}")
    html_page = urlopen(req).read()
    soup = BeautifulSoup(html_page, "html.parser")

    page_num = 0
    offset = 0

    for section in soup.select("div[data-section-index]"):
        section_type = section["data-section-type"]
        section_text = ""
        if section_type in ["pageTitle", "text"]:
            # TODO: Handle hyperlinks
            section_text = section.get_text(strip=True, separator=": ")
        elif section_type == "faqs":
            heading = section.find("h2")
            if heading:
                section_text += heading.get_text(strip=True)
            for qa in section.select("div[class*='dnb-accordion']"):
                question = qa.find("div[class*='dnb-accordion__header']")
                if question:
                    section_text = "\n".join([section_text, question.get_text(strip=True)])
                for elem in qa.find_all(["h3", "ul", "p"]):
                    if elem.name == "ul":
                        for item in elem.find_all("li"):
                            section_text += f"\n- {item.get_text(strip=True)}"
                    else:
                        section_text = "\n".join([section_text, elem.get_text(strip=True)])
        # elif section_type == "comparisonTable":
        #     table_html = "<table>"
        #     table = section.find("table")
        #     for row in table.find_all("tr"):
        #         table_html += "<tr>"
        #         for cell in row.find_all(["td", "th"]):
        #             table_html += f"<{cell.name}>"
        #             content = cell.get_text(strip=True)
        #             # Some cells use checkmarks instead of text
        #             if len(content) == 0 and cell.find("svg"):
        #                 content = "X"
        #
        #             table_html += content
        #             table_html += f"</{cell.name}>"
        #         table_html += "</tr"
        #     table_html += "</table>"
        #
        #     section_text = table_html

        page_map.append((page_num, offset, section_text))
        page_num += 1
        offset += len(section_text)

    return page_map

def get_document_text_from_url(url):
    if args.verbose: print(f"Extracting text from '{url}' using Azure Form Recognizer")
    form_recognizer_client = DocumentAnalysisClient(endpoint=f"https://{args.formrecognizerservice}.cognitiveservices.azure.com/", credential=formrecognizer_creds, headers={"x-ms-useragent": "azure-search-chat-demo/1.0.0"})
    poller = form_recognizer_client.begin_analyze_document_from_url("prebuilt-layout", f"https://{url}")
    form_recognizer_results = poller.result()

    return get_document_text_from_analysis_result(form_recognizer_results)

def get_document_text_from_file(filename):
    if args.localpdfparser:
        reader = PdfReader(filename)
        pages = reader.pages
        offset = 0
        page_map = []
        for page_num, p in enumerate(pages):
            page_text = p.extract_text()
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)

        return page_map
    else:
        if args.verbose: print(f"Extracting text from '{filename}' using Azure Form Recognizer")
        form_recognizer_client = DocumentAnalysisClient(endpoint=f"https://{args.formrecognizerservice}.cognitiveservices.azure.com/", credential=formrecognizer_creds, headers={"x-ms-useragent": "azure-search-chat-demo/1.0.0"})
        with open(filename, "rb") as f:
            poller = form_recognizer_client.begin_analyze_document("prebuilt-layout", document = f)
        form_recognizer_results = poller.result()

        return get_document_text_from_analysis_result(form_recognizer_results)

def split_text(page_map):
    SENTENCE_ENDINGS = [".", "!", "?"]
    WORDS_BREAKS = [",", ";", ":", " ", "(", ")", "[", "]", "{", "}", "\t", "\n"]

    def find_page(offset):
        l = len(page_map)
        for i in range(l - 1):
            if offset >= page_map[i][1] and offset < page_map[i + 1][1]:
                return i
        return l - 1

    all_text = "".join(p[2] for p in page_map)
    length = len(all_text)
    start = 0
    end = length
    while start + SECTION_OVERLAP < length:
        last_word = -1
        end = start + MAX_SECTION_LENGTH

        if end > length:
            end = length
        else:
            # Try to find the end of the sentence
            while end < length and (end - start - MAX_SECTION_LENGTH) < SENTENCE_SEARCH_LIMIT and all_text[end] not in SENTENCE_ENDINGS:
                if all_text[end] in WORDS_BREAKS:
                    last_word = end
                end += 1
            if end < length and all_text[end] not in SENTENCE_ENDINGS and last_word > 0:
                end = last_word # Fall back to at least keeping a whole word
        if end < length:
            end += 1

        # Try to find the start of the sentence or at least a whole word boundary
        last_word = -1
        while start > 0 and start > end - MAX_SECTION_LENGTH - 2 * SENTENCE_SEARCH_LIMIT and all_text[start] not in SENTENCE_ENDINGS:
            if all_text[start] in WORDS_BREAKS:
                last_word = start
            start -= 1
        if all_text[start] not in SENTENCE_ENDINGS and last_word > 0:
            start = last_word
        if start > 0:
            start += 1

        section_text = all_text[start:end]
        yield (section_text, find_page(start))

        last_table_start = section_text.rfind("<table")
        if (last_table_start > 2 * SENTENCE_SEARCH_LIMIT and last_table_start > section_text.rfind("</table")):
            # If the section ends with an unclosed table, we need to start the next section with the table.
            # If table starts inside SENTENCE_SEARCH_LIMIT, we ignore it, as that will cause an infinite loop for tables longer than MAX_SECTION_LENGTH
            # If last table starts inside SECTION_OVERLAP, keep overlapping
            if args.verbose: print(f"Section ends with unclosed table, starting next section with the table at page {find_page(start)} offset {start} table start {last_table_start}")
            start = min(end - SECTION_OVERLAP, start + last_table_start)
        else:
            start = end - SECTION_OVERLAP
        
    if start + SECTION_OVERLAP < end:
        yield (all_text[start:end], find_page(start))

def create_sections_for_file(filename, page_map, description):
    for i, (section, pagenum) in enumerate(split_text(page_map)):
        yield {
            "id": re.sub("[^0-9a-zA-Z_-]","_",f"{filename}-{i}"),
            "content": f"This sections is about {description}. {section}",
            "category": args.category,
            "sourcepage": blob_name_from_file_page(filename, pagenum),
            "sourcefile": filename,
        }

def create_id_from_url(url):
    return re.sub(".pdf", "", os.path.basename(url))

def create_sections_for_webpage(url, page_map, description):
    for (page_num, offset, page_text) in page_map:
        yield {
            "id": f"{create_id_from_url(url)}-{page_num}",
            "content": f"This paragraph is about {description}. {page_text}",
            "category": args.category,
            "sourcepage": blob_name_from_file_page(url, page_num),
            "sourcefile": url,
        }

def create_search_index():
    if args.verbose: print(f"Ensuring search index {args.index} exists")
    index_client = SearchIndexClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                     credential=search_creds)

    if args.index not in index_client.list_index_names():
        index = SearchIndex(
            name=args.index,
            fields=[
                SimpleField(name="id", type="Edm.String", key=True),
                SearchableField(name="content", type="Edm.String", analyzer_name="en.microsoft"),
                SimpleField(name="category", type="Edm.String", filterable=True, facetable=True),
                SimpleField(name="sourcepage", type="Edm.String", filterable=True, facetable=True),
                SimpleField(name="sourcefile", type="Edm.String", filterable=True, facetable=True),
            ],
            semantic_settings=SemanticSettings(
                configurations=[SemanticConfiguration(
                    name='default',
                    prioritized_fields=PrioritizedFields(
                        title_field=None, prioritized_content_fields=[SemanticField(field_name='content')]))])
        )
        if args.verbose: print(f"Creating {args.index} search index")
        index_client.create_index(index)
    else:
        if args.verbose: print(f"Search index {args.index} already exists")

def index_sections(filename, sections):
    if args.verbose: print(f"Indexing sections from '{filename}' into search index '{args.index}'")
    search_client = SearchClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                    index_name=args.index,
                                    credential=search_creds)

    i = 0
    batch = []
    for s in sections:
        batch.append(s)
        i += 1
        if i % 1000 == 0:
            results = search_client.upload_documents(documents=batch)
            succeeded = sum([1 for r in results if r.succeeded])
            if args.verbose: print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")
            batch = []

    if len(batch) > 0:
        results = search_client.upload_documents(documents=batch)
        succeeded = sum([1 for r in results if r.succeeded])
        if args.verbose: print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")

def remove_from_index(filename):
    if args.verbose: print(f"Removing sections from '{filename or '<all>'}' from search index '{args.index}'")
    search_client = SearchClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                    index_name=args.index,
                                    credential=search_creds)
    while True:
        filter = None if filename == None else f"sourcefile eq '{os.path.basename(filename)}'"
        r = search_client.search("", filter=filter, top=1000, include_total_count=True)
        if r.get_count() == 0:
            break
        r = search_client.delete_documents(documents=[{ "id": d["id"] } for d in r])
        if args.verbose: print(f"\tRemoved {len(r)} sections from index")
        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)

if args.removeall:
    remove_blobs(None)
    remove_from_index(None)
else:
    if not args.remove:
        create_search_index()
    
    # print(f"Processing files...")
    # for filename in glob.glob(args.files):
    #     if args.verbose: print(f"Processing '{filename}'")
    #     if args.remove:
    #         remove_blobs(filename)
    #         remove_from_index(filename)
    #     elif args.removeall:
    #         remove_blobs(None)
    #         remove_from_index(None)
    #     else:
    #         if not args.skipblobs:
    #             upload_blobs(filename)
    #         page_map = get_document_text_from_file(filename)
    #         sections = create_sections_for_file(os.path.basename(filename), page_map)
    #         index_sections(os.path.basename(filename), sections)

    # print(f"Processing files...")
    # for source in file_sources:
    #     filename = source[0]
    #     description = source[1]
    #     if args.verbose: print(f"Processing '{filename}'")
    #     if args.remove:
    #         remove_blobs(filename)
    #         remove_from_index(filename)
    #     elif args.removeall:
    #         remove_blobs(None)
    #         remove_from_index(None)
    #     else:
    #         if not args.skipblobs:
    #             upload_blobs(filename)
    #         page_map = get_document_text_from_file(filename)
    #         sections = create_sections_for_file(os.path.basename(filename), page_map, description)
    #         index_sections(os.path.basename(filename), sections)

    print("Processing urls...")
    for source in url_sources:
        url = source[0]
        description = source[1]
        if args.verbose: print(f"Processing '{url}'")

        if ".pdf" in url:
            page_map = get_document_text_from_url(url)
        else:
            page_map = get_html_page_text(url)

        sections = create_sections_for_webpage(url, page_map, description)
        index_sections(os.path.basename(url), sections)
