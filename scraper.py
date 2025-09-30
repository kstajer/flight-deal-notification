import base64
import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openai import OpenAI


BASE_URL = "https://www.fly4free.pl/forum/"
IMG_DIR = "img"
TIMEOUT_IN_MINUTES = 60

prompt = """
You will receive some information about a flight deal. Your task is to extract the required information and return it. All information should be in English.
If there are layovers and a few flights within the deal, then group it (e.g. Warsaw - New York + New York - Los Angles should be grouped into Warsaw - Los Angeles).

The input will in 1-3 parts:
1. Post title (mandatory) - The title often contains a lot of the required columns. It usually starts with the airline(s) shortcutes. Then the destination is often described with IATA airport codes. Sometimes the month is described with roman numbers.
2. Post content (optional) - This is quite random, but can have some extra information.
3. Attached images (optional) - The post author could include some images with additional information.

Required information:
- airlines - a list of airlines that are mentioned in the title/post/images.
- from - the departure city/country (translated from the airport (IATA code)), e.g. "Warsaw, Poland"
- to - the arrival city/country (translated from the airport (IATA code)), e.g. "New York, USA"
- price - in PLN
- when (optional) - if the dates of the deal are provided, transform it into a "Month year" format, e.g. "January 2025". If the year is not speciified, use the next occurence of the month (today is October 2025).

The output MUST follow the following format:
```json
{
    "airlines": [array of strings]
    "from": string,
    "to": string,
    "price": string,
    "when": string
}
```
"""

def main():
    df = pd.read_csv("data.csv")
    print("csv file imported")

    new_posts = check_for_new_posts()
    if new_posts.empty:
        print("No new posts found.")
        return 
    print(f"{new_posts.shape[0]} new posts.")
    df = pd.concat([df, new_posts])
    
    if not df[df.checked == 0].empty:
        new_records = df[df.checked == 0]
        df.loc[df.checked == 0, 'response'] = new_records.apply(lambda x: analyze_record(x), axis=1)
        print('analyzed new posts')
        for record in df.loc[(df.checked == 0) & (df.response.notna()), 'response'].tolist():
            send_notification(_generate_notification_text(record))
            print('sent notification')
        df.loc[(df.checked == 0) & (df.response.notna()), 'checked'] = 1
        print('marked as checked')

    df.to_csv("data.csv", index=False)
    print('saved to csv')


def send_notification(text):
    sender_email = os.getenv("SENDER_EMAIL")
    app_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = "Subject"
    message.attach(MIMEText(text, "plain"))

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(sender_email, app_password)
        server.sendmail(sender_email, receiver_email, message.as_string())
        server.quit()
        print("Email sent successfully!")
    except Exception as e:
        print(f"Error sending email: {e}")

def check_for_new_posts():
    main_url = f"{BASE_URL}promocje-znalazlem-lam-tani-przelot,forum,232"
    response = requests.get(main_url)
    soup = BeautifulSoup(response.content, 'html.parser')

    page_content = soup.find(id='pagecontent')
    titles = page_content.find_all(class_='topictitle')

    data = []

    for x in titles:
        url = x.get('href')
        created_at = _convert_pl_timestamp(x.get('title'))
        if created_at is not None and created_at > datetime.now() - timedelta(minutes=TIMEOUT_IN_MINUTES):
            post_content, img_count = _get_post_details(url)
            data.append({'title': x.text, "created_at": created_at, 'url': url, 'content': post_content, 'img_count': int(img_count), "response": None, "checked": 0})
        else:
            post_content, img_count = "", 0

    if data:
        return pd.DataFrame(data).sort_values(by='created_at', ascending=False).reset_index(drop=True)
    return pd.DataFrame()

def analyze_record(record):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    full_prompt = prompt + f"Post title: {record.title}\nPost content: {record.content}"
    content = [{"type": "text", "text": full_prompt}]

    for i in range(1, int(record.img_count) + 1):
        img_path = f"{IMG_DIR}/{record.url}/file{i}.jpg"
        content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{_encode_image(img_path)}"
                    }
                })

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": content
            }
        ]
    )
    try:
        raw_content = response.choices[0].message.content.replace('```json', '').replace('```', '')
        return json.loads(raw_content)
    except:
        return None

def _convert_pl_timestamp(str_timestamp):
    str_timestamp = str_timestamp.replace("Wysłany: ", "")
    parts = str_timestamp.split(' ')
    if len(parts) != 4:
        return None

    day_str, month_pl, year_str, hour_str = parts

    month_map = {
        'Sty': 1, 'Lut': 2, 'Mar': 3, 'Kwi': 4, 'Maj': 5, 'Cze': 6,
        'Lip': 7, 'Sie': 8, 'Wrz': 9, 'Paź': 10, 'Lis': 11, 'Gru': 12
    }
    month = month_map.get(month_pl)

    if month is None:
        return None

    try:
        datetime_str = f"{day_str} {month} {year_str} {hour_str}"
        datetime_object = datetime.strptime(datetime_str, '%d %m %Y %H:%M')
        return datetime_object
    except ValueError:
        print(f"Could not parse timestamp: {str_timestamp}")
        return None


def _get_post_details(url):
    full_url = f"{BASE_URL}{url}"
    response = requests.get(full_url)
    content = BeautifulSoup(response.content, 'html.parser').find(id="pagecontent")
    post_content = content.find(class_='postbody').text
    img_urls = []

    for e in content.find_all(class_='tablebg'):
        if e.find(class_='tablebg'):
            att = e.find(class_='tablebg')
            for img in att.find_all('img'):
                img_urls.append(img.get('src')[1:])
    i = 1
    for img_url in img_urls:
        full_img_url = BASE_URL + img_url
        dir = os.path.join(IMG_DIR, url)
        if not os.path.exists(dir):
            os.makedirs(dir)

        try:
            img_response = requests.get(full_img_url, stream=True)
            file_name = f"file{i}.jpg"
            file_path = os.path.join(dir, file_name)

            with open(file_path, 'wb') as f:
                for chunk in img_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            i += 1

        except requests.exceptions.RequestException as e:
            print(f"An error occurred while downloading {full_img_url}: {e}")
    return post_content, len(img_urls)



def _generate_notification_text(data):
    return f'A flight from {data["from"].replace(',', '')} to {data["to"].replace(',', '')} in {data['when']} for {data["price"]}'

def _encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

main()
