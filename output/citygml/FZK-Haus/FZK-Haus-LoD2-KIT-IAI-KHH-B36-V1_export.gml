<?xml version='1.0' encoding='UTF-8'?>
<CityModel xmlns="http://www.opengis.net/citygml/2.0" xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0" xmlns:gml="http://www.opengis.net/gml" xmlns:wtr="http://www.opengis.net/citygml/waterbody/2.0" xmlns:app="http://www.opengis.net/citygml/appearance/2.0" xmlns:veg="http://www.opengis.net/citygml/vegetation/2.0" xmlns:dem="http://www.opengis.net/citygml/relief/2.0" xmlns:tran="http://www.opengis.net/citygml/transportation/2.0" xmlns:bldg="http://www.opengis.net/citygml/building/2.0" xmlns:grp="http://www.opengis.net/citygml/cityobjectgroup/2.0" xmlns:tun="http://www.opengis.net/citygml/tunnel/2.0" xmlns:frn="http://www.opengis.net/citygml/cityfurniture/2.0" xmlns:gen="http://www.opengis.net/citygml/generics/2.0" xmlns:brid="http://www.opengis.net/citygml/bridge/2.0" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:luse="http://www.opengis.net/citygml/landuse/2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.opengis.net/citygml/2.0 http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd  http://www.opengis.net/citygml/appearance/2.0 http://schemas.opengis.net/citygml/appearance/2.0/appearance.xsd http://www.opengis.net/citygml/building/2.0 http://schemas.opengis.net/citygml/building/2.0/building.xsd http://www.opengis.net/citygml/generics/2.0 http://schemas.opengis.net/citygml/generics/2.0/generics.xsd">
  <gml:name>AC14-FZK-Haus</gml:name>
  <gml:boundedBy>
    <gml:Envelope srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH" srsDimension="3">
      <gml:lowerCorner srsDimension="3">457842 5439083 111.8</gml:lowerCorner>
      <gml:upperCorner srsDimension="3">457854 5439093 118.317669</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <cityObjectMember>
    <bldg:Building gml:id="UUID_d281adfc-4901-0f52-540b-4cc1a9325f82">
      <gml:description>FZK-Haus (Forschungszentrum Karlsruhe, now KIT), created by Karl-Heinz
                Haefele</gml:description>
      <gml:name>AC14-FZK-Haus</gml:name>
      <creationDate>2017-01-23</creationDate>
      <relativeToTerrain>entirelyAboveTerrain</relativeToTerrain>
      <gen:stringAttribute name="ConstructionMethod">
        <gen:value>New Building</gen:value>
      </gen:stringAttribute>
      <gen:stringAttribute name="IsLandmarked">
        <gen:value>NO</gen:value>
      </gen:stringAttribute>
      <gen:measureAttribute name="GrossPlannedArea">
        <gen:value uom="m2">120.00</gen:value>
      </gen:measureAttribute>
      <bldg:class codeSpace="http://www.sig3d.org/codelists/citygml/2.0/building/2.0/_AbstractBuilding_class.xml">1000</bldg:class>
      <bldg:function codeSpace="http://www.sig3d.org/codelists/citygml/2.0/building/2.0/_AbstractBuilding_function.xml">1000</bldg:function>
      <bldg:usage codeSpace="http://www.sig3d.org/codelists/citygml/2.0/building/2.0/_AbstractBuilding_usage.xml">1000</bldg:usage>
      <bldg:yearOfConstruction>2020</bldg:yearOfConstruction>
      <bldg:roofType codeSpace="http://www.sig3d.org/codelists/citygml/2.0/building/2.0/_AbstractBuilding_roofType.xml">1030</bldg:roofType>
      <bldg:measuredHeight uom="m">6.52</bldg:measuredHeight>
      <bldg:storeysAboveGround>2</bldg:storeysAboveGround>
      <bldg:storeysBelowGround>0</bldg:storeysBelowGround>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="GML_5856d7ad-5e34-498a-817b-9544bfbb1475">
          <gml:name>Outer Wall 1 (West)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_1a859dd0-3573-4556-a3d0-5301a50094d5">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7350_878_759628_120742">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7350_878_759628_120742_0">
                      <gml:posList srsDimension="3">457842 5439088 118.317691453624 457842 5439093 115.430940107676 457842 5439093 111.8 457842 5439083 111.8 457842 5439083 115.430940107676 457842 5439088 118.317691453624</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="GML_d38cf762-c29d-4491-88c9-bdc89e141978">
          <gml:name>Outer Wall 2 (South)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_4e988726-99f9-428d-8526-aa5c60c150e6">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7351_1722_416019_316876">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7351_1722_416019_316876_0">
                      <gml:posList srsDimension="3">457854 5439083 115.430940107676 457842 5439083 115.430940107676 457842 5439083 111.8 457854 5439083 111.8 457854 5439083 115.430940107676</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="GML_8e5db638-e46a-4739-a98a-2fc2d39c9069">
          <gml:name>Outer Wall 3 (East)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_0e7ef216-04e7-42cb-97e6-38addb4623b2">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7352_230_209861_355851">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7352_230_209861_355851_0">
                      <gml:posList srsDimension="3">457854 5439088 118.317691453624 457854 5439083 115.430940107676 457854 5439083 111.8 457854 5439093 111.8 457854 5439093 115.430940107676 457854 5439088 118.317691453624</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:RoofSurface gml:id="GML_875d470b-32b4-4985-a4c8-0f02caa342a2">
          <gml:name>Roof 1 (North)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_6cb03902-7453-476b-9e91-85da8a8a2c0d">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7353_166_774155_320806">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7353_166_774155_320806_0">
                      <gml:posList srsDimension="3">457842 5439088 118.317691453624 457854 5439088 118.317691453624 457854 5439093 115.430940107676 457842 5439093 115.430940107676 457842 5439088 118.317691453624</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:RoofSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="GML_0f30f604-e70d-4dfe-ba35-853bc69609cc">
          <gml:name>Outer Wall 4 (North)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_859d552f-b3e3-4544-b6d6-7b3d341e0fe9">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7354_1362_450904_410226">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7354_1362_450904_410226_0">
                      <gml:posList srsDimension="3">457842 5439093 115.430940107676 457854 5439093 115.430940107676 457854 5439093 111.8 457842 5439093 111.8 457842 5439093 115.430940107676</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:RoofSurface gml:id="GML_eeb6796a-e261-4d3b-a6f2-475940cca80a">
          <gml:name>Roof 2 (South)</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_f22d071b-b75b-486d-85dc-b66cc599ef59">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7355_537_416207_260034">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7355_537_416207_260034_0">
                      <gml:posList srsDimension="3">457854 5439083 115.430940107676 457854 5439088 118.317691453624 457842 5439088 118.317691453624 457842 5439083 115.430940107676 457854 5439083 115.430940107676</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:RoofSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:GroundSurface gml:id="GML_257a8dde-8194-4ca3-b581-abd591dcd6a3">
          <gml:description>Bodenplatte</gml:description>
          <gml:name>Base Surface</gml:name>
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_a73e13a8-9544-4411-9d04-4431c86cba96">
              <gml:surfaceMember>
                <gml:Polygon gml:id="PolyID7356_612_880782_415367">
                  <gml:exterior>
                    <gml:LinearRing gml:id="PolyID7356_612_880782_415367_0">
                      <gml:posList srsDimension="3">457854 5439083 111.8 457842 5439083 111.8 457842 5439093 111.8 457854 5439093 111.8 457854 5439083 111.8</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:GroundSurface>
      </bldg:boundedBy>
      <bldg:lod2Solid>
        <gml:Solid gml:id="solid_UUID_d281adfc-4901-0f52-540b-4cc1a9325f82_lod2">
          <gml:exterior>
            <gml:CompositeSurface gml:id="ID_a215d028-2701-40c4-bce4-303ac1d27541">
              <gml:surfaceMember xlink:href="#PolyID7350_878_759628_120742"/>
              <gml:surfaceMember xlink:href="#PolyID7351_1722_416019_316876"/>
              <gml:surfaceMember xlink:href="#PolyID7352_230_209861_355851"/>
              <gml:surfaceMember xlink:href="#PolyID7353_166_774155_320806"/>
              <gml:surfaceMember xlink:href="#PolyID7354_1362_450904_410226"/>
              <gml:surfaceMember xlink:href="#PolyID7355_537_416207_260034"/>
              <gml:surfaceMember xlink:href="#PolyID7356_612_880782_415367"/>
            </gml:CompositeSurface>
          </gml:exterior>
        </gml:Solid>
      </bldg:lod2Solid>
      <bldg:address xmlns="http://www.opengis.net/citygml/profiles/base/2.0">
        <Address>
          <xalAddress>
            <xAL:AddressDetails>
              <xAL:Locality Type="Town">
                <xAL:LocalityName>Eggenstein-Leopoldshafen</xAL:LocalityName>
                <xAL:Thoroughfare Type="Street">
                  <xAL:ThoroughfareNumber>4711</xAL:ThoroughfareNumber>
                  <xAL:ThoroughfareName>Spöcker Straße</xAL:ThoroughfareName>
                </xAL:Thoroughfare>
                <xAL:PostalCode>
                  <xAL:PostalCodeNumber>76344</xAL:PostalCodeNumber>
                </xAL:PostalCode>
              </xAL:Locality>
            </xAL:AddressDetails>
          </xalAddress>
        </Address>
      </bldg:address>
    </bldg:Building>
  </cityObjectMember>
</CityModel>
