import ollama

response = ollama.chat(
    model='qwen3-vl:2b',
    messages=[
        {
            'role': 'user',
            'content': 'Describe what you see in this image:',
            'images': ['mac.jpeg']  # Path to a local image
        }
    ]
)

print(response['message']['content'])
