import os
import openai
from dotenv import load_dotenv
import logging
import re
import hashlib

from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.llms import AzureOpenAI
from langchain.vectorstores.base import VectorStore
from langchain.chains import ChatVectorDBChain
from langchain.chains import ConversationalRetrievalChain
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.chains.llm import LLMChain
from langchain.chains.chat_vector_db.prompts import CONDENSE_QUESTION_PROMPT
from langchain.prompts import PromptTemplate
from langchain.document_loaders.base import BaseLoader
from langchain.document_loaders import WebBaseLoader
from langchain.text_splitter import TokenTextSplitter, TextSplitter
from langchain.document_loaders.base import BaseLoader
from langchain.document_loaders import TextLoader
from langchain.chat_models import ChatOpenAI
from langchain.schema import AIMessage, HumanMessage, SystemMessage

from utilities.formrecognizer import AzureFormRecognizerClient
from utilities.azureblobstorage import AzureBlobStorageClient
from utilities.translator import AzureTranslatorClient
from utilities.customprompt import PROMPT
from utilities.redis import RedisExtended
from utilities.azuresearch import AzureSearch

import pandas as pd
import urllib

from fake_useragent import UserAgent

class LLMHelper:
    def __init__(self,
        document_loaders : BaseLoader = None, 
        text_splitter: TextSplitter = None,
        embeddings: OpenAIEmbeddings = None,
        llm: AzureOpenAI = None,
        temperature: float = None,
        max_tokens: int = None,
        custom_prompt: str = "",
        vector_store: VectorStore = None,
        k: int = None,
        pdf_parser: AzureFormRecognizerClient = None,
        blob_client: AzureBlobStorageClient = None,
        enable_translation: bool = False,
        translator: AzureTranslatorClient = None):

        load_dotenv()
        openai.api_type = "azure"
        openai.api_base = os.getenv('OPENAI_API_BASE')
        openai.api_version = "2023-03-15-preview"
        openai.api_key = os.getenv("OPENAI_API_KEY")

        # Azure OpenAI settings
        self.api_base = openai.api_base
        self.api_version = openai.api_version
        self.index_name: str = "embeddings"
        self.model: str = os.getenv('OPENAI_EMBEDDINGS_ENGINE_DOC', "text-embedding-ada-002")
        self.deployment_name: str = os.getenv("OPENAI_ENGINE", os.getenv("OPENAI_ENGINES", "text-davinci-003"))
        self.deployment_type: str = os.getenv("OPENAI_DEPLOYMENT_TYPE", "Text")
        self.temperature: float = float(os.getenv("OPENAI_TEMPERATURE", 0.7)) if temperature is None else temperature
        self.max_tokens: int = int(os.getenv("OPENAI_MAX_TOKENS", -1)) if max_tokens is None else max_tokens
        self.prompt = PROMPT if custom_prompt == '' else PromptTemplate(template=custom_prompt, input_variables=["summaries", "question"])
        self.vector_store_type = os.getenv("VECTOR_STORE_TYPE")

        # Azure Search settings
        if  self.vector_store_type == "AzureSearch":
            self.vector_store_address: str = os.getenv('AZURE_SEARCH_SERVICE_NAME')
            self.vector_store_password: str = os.getenv('AZURE_SEARCH_ADMIN_KEY')

        else:
            # Vector store settings
            self.vector_store_address: str = os.getenv('REDIS_ADDRESS', "localhost")
            self.vector_store_port: int= int(os.getenv('REDIS_PORT', 6379))
            self.vector_store_protocol: str = os.getenv("REDIS_PROTOCOL", "redis://")
            self.vector_store_password: str = os.getenv("REDIS_PASSWORD", None)

            if self.vector_store_password:
                self.vector_store_full_address = f"{self.vector_store_protocol}:{self.vector_store_password}@{self.vector_store_address}:{self.vector_store_port}"
            else:
                self.vector_store_full_address = f"{self.vector_store_protocol}{self.vector_store_address}:{self.vector_store_port}"

        self.chunk_size = int(os.getenv('CHUNK_SIZE', 500))
        self.chunk_overlap = int(os.getenv('CHUNK_OVERLAP', 100))
        self.document_loaders: BaseLoader = WebBaseLoader if document_loaders is None else document_loaders
        self.text_splitter: TextSplitter = TokenTextSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap) if text_splitter is None else text_splitter
        self.embeddings: OpenAIEmbeddings = OpenAIEmbeddings(model=self.model, chunk_size=1) if embeddings is None else embeddings
        if self.deployment_type == "Chat":
            self.llm: ChatOpenAI = ChatOpenAI(model_name=self.deployment_name, engine=self.deployment_name, temperature=self.temperature, max_tokens=self.max_tokens if self.max_tokens != -1 else None) if llm is None else llm
        else:
            self.llm: AzureOpenAI = AzureOpenAI(deployment_name=self.deployment_name, temperature=self.temperature, max_tokens=self.max_tokens) if llm is None else llm
        if self.vector_store_type == "AzureSearch":
            self.vector_store: VectorStore = AzureSearch(azure_cognitive_search_name=self.vector_store_address, azure_cognitive_search_key=self.vector_store_password, index_name=self.index_name, embedding_function=self.embeddings.embed_query) if vector_store is None else vector_store
        else:
            self.vector_store: RedisExtended = RedisExtended(redis_url=self.vector_store_full_address, index_name=self.index_name, embedding_function=self.embeddings.embed_query) if vector_store is None else vector_store   
        self.k : int = 3 if k is None else k

        self.pdf_parser : AzureFormRecognizerClient = AzureFormRecognizerClient() if pdf_parser is None else pdf_parser
        self.blob_client: AzureBlobStorageClient = AzureBlobStorageClient() if blob_client is None else blob_client
        self.enable_translation : bool = False if enable_translation is None else enable_translation
        self.translator : AzureTranslatorClient = AzureTranslatorClient() if translator is None else translator

        self.user_agent: UserAgent() = UserAgent()
        self.user_agent.random

    def add_embeddings_lc(self, source_url):
        try:
            documents = self.document_loaders(source_url).load()
            
            # Convert to UTF-8 encoding for non-ascii text
            for(document) in documents:
                try:
                    if document.page_content.encode("iso-8859-1") == document.page_content.encode("latin-1"):
                        document.page_content = document.page_content.encode("iso-8859-1").decode("utf-8", errors="ignore")
                except:
                    pass
                
            docs = self.text_splitter.split_documents(documents)
            
            # Remove half non-ascii character from start/end of doc content (langchain TokenTextSplitter may split a non-ascii character in half)
            pattern = re.compile(r'[\x00-\x1f\x7f\u0080-\u00a0\u2000-\u3000\ufff0-\uffff]')
            for(doc) in docs:
                doc.page_content = re.sub(pattern, '', doc.page_content)
                if doc.page_content == '':
                    docs.remove(doc)
            
            keys = []
            for i, doc in enumerate(docs):
                # Create a unique key for the document
                source_url = source_url.split('?')[0]
                filename = "/".join(source_url.split('/')[4:])
                hash_key = hashlib.sha1(f"{source_url}_{i}".encode('utf-8')).hexdigest()
                hash_key = f"doc:{self.index_name}:{hash_key}"
                keys.append(hash_key)
                doc.metadata = {"source": f"[{source_url}]({source_url}_SAS_TOKEN_PLACEHOLDER_)" , "chunk": i, "key": hash_key, "filename": filename}
                doc.document_identifier = "Smile"
            if self.vector_store_type == 'AzureSearch':
                self.vector_store.add_documents(documents=docs, keys=keys)
            else:
                self.vector_store.add_documents(documents=docs, redis_url=self.vector_store_full_address,  index_name=self.index_name, keys=keys)
            
        except Exception as e:
            logging.error(f"Error adding embeddings for {source_url}: {e}")
            raise e

    def convert_file_and_add_embeddings(self, source_url, filename, enable_translation=False):
        # Extract the text from the file
        text = self.pdf_parser.analyze_read(source_url)
        # Translate if requested
        text = list(map(lambda x: self.translator.translate(x), text)) if self.enable_translation else text

        # Upload the text to Azure Blob Storage
        converted_filename = f"converted/{filename}.txt"
        source_url = self.blob_client.upload_file("\n".join(text), f"converted/{filename}.txt", content_type='text/plain; charset=utf-8')

        print(f"Converted file uploaded to {source_url} with filename {filename}")
        # Update the metadata to indicate that the file has been converted
        self.blob_client.upsert_blob_metadata(filename, {"converted": "true"})

        self.add_embeddings_lc(source_url=source_url)

        return converted_filename

    def get_all_documents(self, k: int = None):
        result = self.vector_store.similarity_search(query="*", k= k if k else self.k)
        return pd.DataFrame(list(map(lambda x: {
                'key': x.metadata['key'],
                'filename': x.metadata['filename'],
                'source': urllib.parse.unquote(x.metadata['source']), 
                'content': x.page_content, 
                'metadata' : x.metadata,
                }, result)))

    def get_semantic_answer_lang_chain(self, question, chat_history):
        question_generator = LLMChain(llm=self.llm, prompt=CONDENSE_QUESTION_PROMPT, verbose=False)
        doc_chain = load_qa_with_sources_chain(self.llm, chain_type="stuff", verbose=True, prompt=self.prompt)
        chain = ConversationalRetrievalChain(
            retriever=self.vector_store.as_retriever(),
            question_generator=question_generator,
            combine_docs_chain=doc_chain,
            return_source_documents=True,
            # top_k_docs_for_context= self.k
        )
        result = chain({"question": question, "chat_history": chat_history})
        context = "\n".join(list(map(lambda x: x.page_content, result['source_documents'])))
        sources = "\n".join(set(map(lambda x: x.metadata["source"], result['source_documents'])))

        container_sas = self.blob_client.get_container_sas()
        
        result['answer'] = result['answer'].split('SOURCES:')[0].split('Sources:')[0].split('SOURCE:')[0].split('Source:')[0]
        sources = sources.replace('_SAS_TOKEN_PLACEHOLDER_', container_sas)

        return question, result['answer'], context, sources

    def get_embeddings_model(self):
        OPENAI_EMBEDDINGS_ENGINE_DOC = os.getenv('OPENAI_EMEBDDINGS_ENGINE', os.getenv('OPENAI_EMBEDDINGS_ENGINE_DOC', 'text-embedding-ada-002'))  
        OPENAI_EMBEDDINGS_ENGINE_QUERY = os.getenv('OPENAI_EMEBDDINGS_ENGINE', os.getenv('OPENAI_EMBEDDINGS_ENGINE_QUERY', 'text-embedding-ada-002'))
        return {
            "doc": OPENAI_EMBEDDINGS_ENGINE_DOC,
            "query": OPENAI_EMBEDDINGS_ENGINE_QUERY
        }

    def get_completion(self, prompt, **kwargs):
        if self.deployment_type == 'Chat':
            return self.llm([HumanMessage(content=prompt)]).content
        else:
            return self.llm(prompt)
