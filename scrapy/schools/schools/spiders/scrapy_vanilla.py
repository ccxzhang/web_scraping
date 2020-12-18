"""
Iterate over a CSV of URLs, recursively gather their within-domain links, parse the text of each page, and save to file

CSV is expected to be structured with a school ID (called NCES school number of NCESSCH) and URL like so:

|               NCESSCH | URL_2019                                                 |
|----------------------:|:---------------------------------------------------------|
|   1221763800065630210 | http://www.charlottesecondary.org/                       |
|   1223532959313072128 | http://www.kippcharlotte.org/                            |
|   1232324303569510400 | http://www.socratesacademy.us/                           |
|   1226732686900957185 | https://ggcs.cyberschool.com/                            |
|   1225558292157620224 | http://www.emmajewelcharter.com/pages/Emma_Jewel_Charter |

USAGE
    Pass in the start_urls from a a csv file.
    For example, within web_scraping/scrapy/schools/school, run:
    
        scrapy crawl schoolspider -a csv_input=spiders/test_urls.csv
        
    To append output to a file, run:
        
        scrapy crawl schoolspider -a csv_input=spiders/test_urls.csv -o schoolspider_output.json
   
    This output can be saved into other file types as well. Output can also be saved
    in MongoDb (see MongoDBPipeline in pipelines.py).
    
    NOTE: -o will APPEND output. This can be misleading(!) when debugging since the output
          file may contain more than just the most recent output.

    # Run spider with logging, and append to an output JSON file
    scrapy runspider generic.py \
        -L WARNING \
        -o school_output_test.json \
        -a input=test_urls.csv

    # Run spider in the background with `nohup`
    nohup scrapy runspider generic.py \
        -L WARNING \
        -o school_output_test.json \
        -a input=test_urls.csv &

CREDITS
    Inspired by script in this private repo: https://github.com/lisasingh/covid-19/blob/master/scraping/generic.py

TODO
    - Indicate failed responses -- currently it simply does not append to output
    - Implement middleware for backup crawling of failed cases
    - Configure for distributed crawling with Spark & Hadoop
    - Configure for historical crawling with Internet Archive's Wayback Machine API
"""

# The follow two imports are 3rd party libraries
import tldextract
import regex

import csv
from bs4 import BeautifulSoup # BS reads and parses even poorly/unreliably coded HTML 
from bs4.element import Comment # helps with detecting inline/junk tags when parsing with BS
import html5lib # slower but more accurate bs4 parser for messy HTML
from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import Rule, CrawlSpider

from schools.items import CharterItem

# The following are required for parsing File text
import io
import os
from schools.items import CharterItem
from tempfile import NamedTemporaryFile
import textract
from itertools import chain
import re
from urllib.parse import urlparse
import requests


# Used for extracting text from PDFs
control_chars = ''.join(map(chr, chain(range(0, 9), range(11, 32), range(127, 160))))
CONTROL_CHAR_RE = re.compile('[%s]' % re.escape(control_chars))
TEXTRACT_EXTENSIONS = [".pdf", ".doc", ".docx", ""]

# Define inline tags for cleaning out HTML
inline_tags = ["b", "big", "i", "small", "tt", "abbr", "acronym", "cite", "dfn",
               "em", "kbd", "strong", "samp", "var", "bdo", "map", "object", "q",
               "span", "sub", "sup"]


class CustomLinkExtractor(LinkExtractor):
    def __init__(self, *args, **kwargs):
        super(CustomLinkExtractor, self).__init__(*args, **kwargs)
        # Keep the default values in "deny_extensions" *except* for those types we want
        self.deny_extensions = [ext for ext in self.deny_extensions if ext not in TEXTRACT_EXTENSIONS]


class CharterSchoolSpider(CrawlSpider):
    name = 'schoolspider'
    rules = [
        Rule(
            LinkExtractor(
                canonicalize=False,
                unique=True
            ),
            follow=True,
            callback="parse_items"
        )
    ]
    def __init__(self, csv_input=None, *args, **kwargs):
        """
        Overrides default constructor to set custom
        instance attributes.
        
        Attributes:
        
        - start_urls:
            Used by scrapy.spiders.Spider. A list of URLs where the
            spider will begin to crawl.

        - allowed_domains:
            Used by scrapy.spiders.Spider. An optional list of
            strings containing domains that this spider is allowed
            to crawl.

        - domain_to_id:
            A custom attribute used to map a string domain to
            a number representing the school id defined by
            csv_input.
        """
        super(CharterSchoolSpider, self).__init__(*args, **kwargs)
        self.start_urls = []
        self.allowed_domains = []
        self.rules = (Rule(CustomLinkExtractor(), follow=True, callback="parse_items"),)
        self.domain_to_id = {}
        self.init_from_csv(csv_input)
        

    # note: make sure we ignore robot.txt
    # Method for parsing items
    def parse_items(self, response):

        item = CharterItem()
        item['url'] = response.url
        item['text'] = self.get_text(response)
        domain = self.get_domain(response.url)
        item['school_id'] = self.domain_to_id[domain]
        # uses DepthMiddleware
        item['depth'] = response.request.meta['depth']
        
        # iterate over the list of images and append urls for downloading
        item['image_urls'] = []
        for image in response.xpath('//img/@src').extract():
            # make each one into a full URL and add to item[]
            item['image_urls'].append(response.urljoin(image))
        
        # iterate over list of links with .pdf/.doc/.docx in them and appends urls for downloading
        item['file_urls'] = []
        selector = 'a[href$=".pdf"]::attr(href), a[href$=".doc"]::attr(href), a[href$=".docx"]::attr(href)'
        print("PDF FOUND", response.css(selector).extract())
        
        item['file_text'] = []
        for href in response.css(selector).extract():
            # Check if href is complete.
            if "http" not in href:
                href = "http://" + domain + href
            # Add file URL to pipeline
            item['file_urls'] += [href]
            
            # Parse file text and it to list of file texts
            item['file_text'] += [self.parse_file(href, item['url'])]
            
        yield item    
        
        
    def init_from_csv(self, csv_input):
        """
        Generate's this spider's instance attributes
        from the input CSV file.
        
        CSV's format:
        1. The first row is meta data that is ignored.
        2. Rows in the csv are 1d arrays with one element.
        ex: row == ['3.70014E+11,http://www.charlottesecondary.org/'].
        
        Note: start_requests() isn't used since it doesn't work
        well with CrawlSpider Rules.
        
        Args:
            csv_input: Is the path string to this file.
        Returns:
            Nothing is returned. However, start_urls,
            allowed_domains, and domain_to_id are initialized.
        """
        if not csv_input:
            return
        with open(csv_input, 'r') as f:
            reader = csv.reader(f, delimiter="\t",quoting=csv.QUOTE_NONE)
            first_row = True
            for raw_row in reader:
                if first_row:
                    first_row = False
                    continue
                csv_row = raw_row[0]
                school_id, url = csv_row.split(",")
                domain = self.get_domain(url)
                # set instance attributes
                self.start_urls.append(url)
                self.allowed_domains.append(domain)
                # note: float('3.70014E+11') == 370014000000.0
                self.domain_to_id[domain] = float(school_id)

                
    def get_domain(self, url):
        """
        Given the url, gets the top level domain using the
        tldextract library.
        
        Ex:
        >>> get_domain('http://www.charlottesecondary.org/')
        charlottesecondary.org
        >>> get_domain('https://www.socratesacademy.us/our-school')
        socratesacademy.us
        """
        extracted = tldextract.extract(url)
        return f'{extracted.domain}.{extracted.suffix}'
    
    
    def get_text(self, response):
        """
        Gets the readable text from a website's body and filters it.
        Ex:
        if response.body == "\u00a0OUR \tSCHOOL\t\t\tPARENTSACADEMICSSUPPORT \u200b\u200bOur Mission"
        >>> get_text(response)
        'OUR SCHOOL PARENTSACADEMICSSUPPORT Our Mission'
        
        For another example, see filter_text_ex.txt
        """
        # Load HTML into BeautifulSoup, extract text
        soup = BeautifulSoup(response.body, 'html5lib') # slower but more accurate parser for messy HTML
        # Remove non-visible tags from soup
        [s.extract() for s in soup(['head', 'title', '[document]'])]
        # Extract text, remove <p> tags
        visible_text = soup.get_text(strip = True) # removes extra white spaces from each text chunk; splits by space
        
        # Remove inline tags from text
        for it in inline_tags:
            visible_text = visible_text.replace("<" + it + ">", "")
            visible_text = visible_text.replace("</" + it + ">", "")
        # Remove ascii (such as "\u00")
        filtered_text = visible_text.encode('ascii', 'ignore').decode('ascii').encode('utf-8').decode('utf-8')
        
        # Replace all consecutive spaces (including in unicode), tabs, or "|"s with a single space
        filtered_text = regex.sub(r"[ \t\h\|]+", " ", filtered_text)
        # Replace any consecutive linebreaks with a single newline
        filtered_text = regex.sub(r"[\n\r\f\v]+", "\n", filtered_text)
        # Remove json strings: https://stackoverflow.com/questions/21994677/find-json-strings-in-a-string
        # Uses the regex 3rd party library to support recursive Regex
        filtered_text = regex.sub(r"{(?:[^{}]*|(?R))*}", " ", filtered_text)

        # Remove white spaces at beginning and end of string; return
        return filtered_text.strip()

    
    def parse_file(self, href, parent_url):
        """
        Given the file's url and its parent url, 
        scrape the text from the file and return it. 
        This will also create a .txt file within the user's subdirectory.
        At the top of this .txt file, you will also see the file's Base URL, Parent URL, and File URL. 
        
        Ex:
        >>> parse_pdf('https://www.imagescape.com/media/uploads/zinnia/2018/08/20/sampletext.pdf',
                'https://www.imagescape.com/media/uploads/zinnia/2018/08/20/scrape_me.html')
            
            Base URL: imagescape.com
            Parent URL: https://www.imagescape.com/media/uploads/zinnia/2018/08/20/scrape_me.html
            File URL: https://www.imagescape.com/media/uploads/zinnia/2018/08/20/sampletext.pdf
            "This is a caterwauling test of a transcendental PDF."
        
        """

        # Parse text from file and add to .txt file AND item
        response_href = requests.get(href)

        extension = list(filter(lambda x: response_href.url.lower().endswith(x), TEXTRACT_EXTENSIONS))[0]
      

        tempfile = NamedTemporaryFile(suffix=extension)
        tempfile.write(response_href.content)
        tempfile.flush()

        extracted_data = textract.process(tempfile.name)
        extracted_data = extracted_data.decode('utf-8')
        extracted_data = CONTROL_CHAR_RE.sub('', extracted_data)
        tempfile.close()
        base_url = self.get_domain(parent_url)
        
        # Create a filepath for the .txt file
        txt_file_name = "files" + "/" + base_url + "/" + os.path.basename(urlparse(href).path).replace(extension, ".txt")
        
        # If subdirectory does not exist yet, create it
        if not os.path.isdir("files" + "/" + base_url):
            os.mkdir("files" + "/" + base_url)
            
        with open(txt_file_name, "w") as f:

            f.write("Base URL: " + base_url)
            f.write("\n")
            f.write("Parent URL: " + parent_url)
            f.write("\n")
            f.write("File URL: " + response_href.url.upper())

            f.write("\n")
            f.write(extracted_data)
            f.write("\n\n")
            
        return extracted_data 
    


    

